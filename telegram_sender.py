"""Small Telegram HTML sender, adapted from AI-Trading-Desk/core/telegram_sender.py.

Verified TLS, bounded POST/response and exact acknowledgement checks only.
No callbacks, polling, outbox, scheduling, redirects or automatic retries.
Importing this module reads no configuration and performs no network operation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import http.client
import json
import math
from pathlib import Path
import re
import socket
import ssl
import sys
import time

MAX_RESPONSE_BYTES = 65536
MAX_MESSAGES = 20
MAX_HTML_UNITS = 4096
DISCLAIMER = "Conditional setup — wait for confirmation."
SETUP_FIELDS = ("rank", "direction", "strategy_type", "reasoning", "entry_area", "confirmation",
                "invalidation", "target1", "target2", "risk_reward", "cancel_conditions")


class TelegramError(ValueError):
    """Only application-owned messages, never URLs/tokens/server descriptions."""


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)

    def __post_init__(self):
        # Reuse the previous project's strict token/numeric destination checks.
        if not isinstance(self.bot_token, str) or not re.fullmatch(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,200}", self.bot_token):
            raise TelegramError("Invalid bot_token in telegram_config.local.json.")
        if (not isinstance(self.chat_id, str) or not re.fullmatch(r"-?[1-9][0-9]{0,15}", self.chat_id)
                or abs(int(self.chat_id)) > 2**52 - 1):
            raise TelegramError("Set chat_id to the exact numeric destination ID as a JSON string.")


@dataclass(frozen=True)
class DeliveryOutcome:
    status: str
    accepted_parts: int = 0
    required_parts: int = 0
    message_ids: tuple = ()
    error: str = ""

    @property
    def accepted(self):
        return (self.status == "accepted" and self.required_parts > 0
                and self.accepted_parts == self.required_parts
                and len(self.message_ids) == self.required_parts
                and len(set(self.message_ids)) == self.required_parts
                and all(type(eid) is int and eid > 0 for eid in self.message_ids))


def local_config_path():
    # Never resolve relative to PyCharm, Explorer's CWD or PyInstaller _MEIPASS.
    root = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    return root / "telegram_config.local.json"


def load_config(path=None):
    try:
        data = json.loads((Path(path) if path is not None else local_config_path()).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TelegramError("Missing telegram_config.local.json beside main.py or the EXE.") from None
    except (OSError, ValueError, UnicodeError):
        raise TelegramError("Cannot read a valid telegram_config.local.json. Check its JSON format and permissions.") from None
    if not isinstance(data, dict) or set(data) != {"bot_token", "chat_id"}:
        raise TelegramError("Telegram configuration must contain only bot_token and chat_id.")
    return TelegramConfig(data["bot_token"], data["chat_id"])


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _paragraphs(text, bold=False, limit=3000):
    """Split raw text first; escape/wrap each fragment so HTML stays balanced."""
    while text:
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if _units(html.escape(text[:mid], quote=True)) + (7 if bold else 0) <= limit:
                low = mid
            else:
                high = mid - 1
        if low == 0:
            raise TelegramError("A report character cannot be encoded within Telegram's message limit.")
        cut = low
        if cut < len(text):
            boundary = max(text.rfind("\n", 0, cut), text.rfind(" ", 0, cut))
            if boundary >= cut // 2:
                cut = boundary + 1
        escaped = html.escape(text[:cut], quote=True)
        yield f"<b>{escaped}</b>" if bold else escaped
        text = text[cut:]


def format_trade_messages(report, symbol, timeframes, *, chart_verified, timestamp=None):
    """Only the validated human fields are used; unknown/internal fields ignored."""
    if isinstance(report,dict) and report.get('analysis_kind')=='simple_smc':
        return format_simple_smc_message(report,symbol)
    if isinstance(report, dict) and report.get('analysis_kind') == 'elliott_wave':
        return format_elliott_messages(report, symbol, chart_verified=chart_verified, timestamp=timestamp)
    if (not isinstance(report, dict) or not isinstance(report.get("final_analysis"), str)
            or len(report["final_analysis"].strip()) < 40 or not isinstance(report.get("setups"), list)
            or len(report["setups"]) > 3):
        raise TelegramError("No validated completed trade analysis is available for Telegram.")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+", symbol):
        raise TelegramError("Telegram report requires the exact exchange-qualified symbol.")
    if not isinstance(timeframes, (list, tuple)) or not timeframes or any(tf not in ("1H", "4H", "1D", "15m") for tf in timeframes):
        raise TelegramError("Telegram report has invalid analysis timeframes.")
    timestamp = datetime.now(timezone.utc) if timestamp is None else timestamp
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise TelegramError("An explicit timezone is required for the analysis timestamp.")
    setups = report["setups"]
    if not setups and not re.search(r"\b(?:NO TRADE|WAIT)\b", report["final_analysis"], re.I):
        raise TelegramError("Zero setups must include the final NO TRADE / WAIT assessment.")
    blocks = [(f"Symbol: {symbol}", True),
              (f"Analysis: {timestamp.isoformat(timespec='seconds')}\nTimeframes: {' / '.join(timeframes)}", False)]
    if not chart_verified:
        blocks.append(("Chart drawings/groups: NOT VERIFIED — operator review required. "
                       "Analysis and delivery do not confirm chart completion.", False))
    ranks = ("Primary", "Secondary", "Alternative")
    for index, setup in enumerate(setups, 1):
        if (not isinstance(setup, dict) or type(setup.get("rank")) is not int or setup["rank"] != index
                or setup.get("direction") not in ("LONG", "SHORT")):
            raise TelegramError("Invalid validated setup ranking or direction.")
        for key in SETUP_FIELDS[2:]:
            if key in ("target2", "risk_reward") and setup.get(key) is None:
                continue
            if not isinstance(setup.get(key), str) or not setup[key].strip():
                raise TelegramError("A required human-readable setup field is missing.")
        reasoning = setup['reasoning']
        confirmation = setup['confirmation']
        # Keep structured caveats verbatim inside the requested fields. Risk/reward
        # can contain entry restrictions, so do not guess which words are safe to drop.
        if setup.get('risk_reward') and setup['risk_reward'].strip().casefold() not in reasoning.casefold():
            reasoning += " " + setup['risk_reward']
        if setup['cancel_conditions'].strip().casefold() not in confirmation.casefold():
            confirmation += " Cancel / stand aside: " + setup['cancel_conditions']
        blocks += [(f"Setup {index} — {ranks[index-1]} | {setup['direction']}", True),
                   (f"Strategy: {setup['strategy_type']}", False), (f"Reasoning: {reasoning}", False),
                   (f"Entry area: {setup['entry_area']}", True), (f"Confirmation: {confirmation}", False),
                   (f"Stop / invalidation: {setup['invalidation']}", True), (f"Target 1: {setup['target1']}", True)]
        if setup.get("target2") is not None:
            blocks.append((f"Target 2: {setup['target2']}", True))
    if not setups:
        # No structured setup exists: its final WAIT explanation supplies the reason.
        blocks += [("NO TRADE / WAIT", True), (report["final_analysis"], False)]
    header = "<b>TRADE WITH AM — PRICE ACTION TRADING AGENT</b>\n\n"
    footer = "\n\n<i>" + DISCLAIMER + "</i>"
    # Check the complete compact message first; reserve numbering only if splitting
    # is actually necessary. Retain the conservative raw-HTML length safeguard.
    single = header + "\n\n".join(
        f"<b>{html.escape(text, quote=True)}</b>" if bold else html.escape(text, quote=True)
        for text, bold in blocks) + footer
    if _units(single) <= MAX_HTML_UNITS:
        return [single]
    budget = MAX_HTML_UNITS - _units(header + "Message 20/20\n\n" + footer)
    bodies, body = [], ""
    for text, bold in blocks:
        for piece in _paragraphs(text, bold, limit=budget):
            if body and _units(body + "\n\n" + piece) > budget:
                bodies.append(body)
                body = ""
                if len(bodies) >= MAX_MESSAGES:
                    raise TelegramError("Report exceeds the 20-message safety limit. Use Copy for manual retrieval; nothing was sent.")
            body += ("\n\n" if body else "") + piece
    if body:
        bodies.append(body)
    if not bodies or len(bodies) > MAX_MESSAGES:
        raise TelegramError("Report exceeds the 20-message safety limit. Use Copy for manual retrieval; nothing was sent.")
    messages = [header + f"Message {n}/{len(bodies)}\n\n" + body + footer for n,body in enumerate(bodies, 1)]
    if any(_units(text) > MAX_HTML_UNITS for text in messages):
        raise TelegramError("Report exceeded Telegram's message limit; nothing was sent.")
    return messages


def format_elliott_messages(report, symbol, *, chart_verified, timestamp=None):
    """Compact verified primary scenarios/conditional ideas; reuse HTML packing."""
    if chart_verified is not True or not isinstance(report.get('phases'), dict):
        raise TelegramError('Elliott chart/scenario verification is required before Telegram.')
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+',symbol):
        raise TelegramError('Exact symbol required for Elliott report.')
    categories = report.get('categories')
    if not isinstance(categories,list) or not categories or any(c not in ('swing','intraday') for c in categories):
        raise TelegramError('Invalid Elliott analysis type.')
    blocks = [(f'Symbol: {symbol}',True),
              ('Analysis: ' + ('Both' if len(categories)==2 else categories[0].title()),False)]
    ideas = 0
    for category in categories:
        phase=report['phases'].get(category)
        if not isinstance(phase,dict) or not isinstance(phase.get('setups'),list) or len(phase['setups'])>3:
            raise TelegramError('Missing verified category results.')
        scenarios=phase['setups']
        blocks.append((f'{category.title()}: {len(scenarios)} scenarios',True))
        if scenarios:
            primary=scenarios[0]
            for key,label in (('expected_path','Primary'),('fibonacci_context','Fibonacci'),('confirmation','Confirm'),('invalidation','Invalidate')):
                if not isinstance(primary.get(key),str) or not primary[key].strip():
                    raise TelegramError('Missing primary scenario report field.')
                blocks.append((label+': '+primary[key],False))
        else:
            blocks.append(('NO CLEAR SCENARIO / WAIT — no defensible scenario qualified. See the application for the analysis.',False))
        best=next((s for s in scenarios if s.get('direction') in ('LONG','SHORT')),None)
        if best is None:
            blocks.append((f'{category.title()}: NO TRADE / WAIT',True))
        else:
            ideas+=1
            blocks.append((f"Conditional {category.title()} {best['direction']} (Scenario {best['rank']})",True))
            for key,label in (('conditional_trade_idea','Idea'),('entry_area','Entry area'),('confirmation','Requires'),
                              ('invalidation','Invalidation'),('target1','Target'),('target2','Target 2')):
                if best.get(key) is not None: blocks.append((label+': '+best[key],False))
    if not ideas: blocks.append(('NO TRADE / WAIT — no attractive confirmed entry is implied by these scenarios.',True))
    header='<b>Trade With AM — Elliott Wave &amp; Fibonacci Trading Agent</b>\n\n'
    footer='\n\n<i>Conditional analysis — wait for confirmation. No orders.</i>'
    budget=MAX_HTML_UNITS-_units(header+'Message 2/2\n\n'+footer)
    bodies=[];body=''
    for text,bold in blocks:
        for part in _paragraphs(text,bold,limit=budget):
            if body and _units(body+'\n\n'+part)>budget:
                bodies.append(body);body=''
            body+= ('\n\n' if body else '')+part
    if body:bodies.append(body)
    if not bodies or len(bodies)>2:
        raise TelegramError('Elliott report exceeds the compact two-message limit; use Copy. Nothing sent.')
    return [header+(f'Message {i}/{len(bodies)}\n\n' if len(bodies)>1 else '')+body+footer for i,body in enumerate(bodies,1)]


def simple_analysis_complete(outcome):
    """Notification gate only; no interpretation of drawings or trade quality."""
    return (outcome.status=='completed_response' and outcome.analysis_completed is True and outcome.return_code==0
            and isinstance(outcome.final_analysis,str) and bool(outcome.final_analysis.strip())
            and not re.search(r'\b(?:analysis|execution|run)\s+(?:(?:is|was)\s+)?(?:incomplete|interrupted|aborted)\b|'
                r'\b(?:analysis|execution)\s+(?:did not|could not)\s+(?:finish|complete)\b',outcome.final_analysis,re.I))


def _markdown_inline(text, bold=False):
    # Keep link labels AND destinations as readable text; the existing sender's
    # acknowledgement checker already supports the b/i tags used here.
    text=re.sub(r'!?\[([^\]\n]*)\]\(([^)\n]*)\)',lambda m:m[1]+' ('+m[2]+')',text)
    pieces=re.split(r'(\*\*[^\n]+?\*\*)',text)
    if bold:
        return '<b>'+html.escape(''.join(p[2:-2] if p.startswith('**') and p.endswith('**') else p for p in pieces),quote=True)+'</b>'
    return ''.join('<b>'+html.escape(p[2:-2],quote=True)+'</b>' if p.startswith('**') and p.endswith('**')
                   else html.escape(p,quote=True) for p in pieces)


def _format_complete_markdown(text):
    """Syntax formatting only. No analysis selection, omission or rewriting."""
    lines=text.replace('\r\n','\n').replace('\r','\n').split('\n')
    output=[];i=0
    def cells(line):
        line=line.strip()
        if line.startswith('|'):line=line[1:]
        if line.endswith('|') and not line.endswith(r'\|'):line=line[:-1]
        return re.split(r'(?<!\\)\|',line)
    while i<len(lines):
        line=lines[i]
        if (i+1<len(lines) and '|' in line and '-' in lines[i+1]
                and re.fullmatch(r'[\s|:\-]+',lines[i+1])):
            headers=cells(line);j=i+2;rows=[]
            while j<len(lines) and '|' in lines[j] and lines[j].strip():
                rows.append(cells(lines[j]));j+=1
            if rows and all(len(row)==len(headers) for row in rows):
                output.append('')
                for row in rows:
                    for label,value in zip(headers,row):
                        label=label.strip().replace(r'\|','|');value=value.strip().replace(r'\|','|')
                        output.append(_markdown_inline(label,bold=True)+': '+_markdown_inline(value))
                    output.append('')
                i=j;continue
            # An unfamiliar/ragged table stays literal; never discard its cells.
        heading=re.match(r'^\s{0,3}#{1,6}\s+(.*)$',line)
        if heading:
            output.extend(('',_markdown_inline(heading[1],bold=True),''))
        elif re.fullmatch(r'\s*\*\*.+\*\*\s*',line):
            output.extend(('',_markdown_inline(line),''))
        else:
            bullet=re.match(r'^(\s*)[-*+]\s+(.*)$',line)
            output.append(_markdown_inline(bullet[1]+'• '+bullet[2] if bullet else line))
        i+=1
    return '\n'.join(output)


def _split_formatted_response(body, header):
    """Split text at boundaries, regenerating balanced tags for every part."""
    single=header+'\n\n'+body
    if _units(single)<=MAX_HTML_UNITS:return [single]
    runs=[];position=0;bold=False
    for token in re.split(r'(<b>|</b>)',body):
        if token=='<b>':bold=True
        elif token=='</b>':bold=False
        elif token:
            value=html.unescape(token)
            runs.append((position,position+len(value),value,bold));position+=len(value)
    plain=''.join(run[2] for run in runs)
    # At least one source character per part: this reserves enough digits for
    # numbering without limiting or truncating the number of source parts.
    digits=len(str(len(plain)))
    budget=MAX_HTML_UNITS-_units(header+'\n('+'9'*digits+'/'+'9'*digits+')\n\n')
    if budget<16:raise TelegramError('Telegram header leaves no room for the response; nothing sent.')

    def render(start,end):
        pieces=[]
        for left,right,value,strong in runs:
            if right<=start:continue
            if left>=end:break
            piece=html.escape(value[max(0,start-left):min(right,end)-left],quote=True)
            pieces.append('<b>'+piece+'</b>' if strong else piece)
        return ''.join(pieces)

    bodies=[];start=0
    while start<len(plain):
        low=start;high=min(len(plain),start+budget)
        while low<high:
            middle=(low+high+1)//2
            if _units(render(start,middle))<=budget:low=middle
            else:high=middle-1
        end=low
        if end==start:raise TelegramError('A response character cannot fit Telegram; nothing sent.')
        if end<len(plain):
            minimum=start+max(1,(end-start)//3)
            paragraph=plain.rfind('\n\n',minimum,end)
            sentences=list(re.finditer(r'[.!?](?:["\u201d\u2019)]*)\s+',plain[minimum:end]))
            newline=plain.rfind('\n',minimum,end)
            space=plain.rfind(' ',minimum,end)
            if paragraph>=minimum:end=paragraph+2
            elif sentences:end=minimum+sentences[-1].end()
            elif newline>=minimum:end=newline+1
            elif space>=minimum:end=space+1
            # Only an unbroken oversized token/sentence needs a harder split.
        bodies.append(render(start,end));start=end
    messages=[header+f'\n({index}/{len(bodies)})\n\n'+part for index,part in enumerate(bodies,1)]
    if any(_units(message)>MAX_HTML_UNITS for message in messages):
        raise TelegramError('Telegram part exceeded the message limit; nothing sent.')
    return messages


def format_smc_response(response_text, symbol, analysis_type='SMC Intraday'):
    """Reuse the complete-response Markdown formatter and multipart splitter."""
    if (not isinstance(response_text,str) or not response_text.strip()
            or not isinstance(symbol,str) or not re.fullmatch(r'(?:[A-Za-z0-9_]+:)?[A-Za-z0-9_.!/-]+',symbol)
            or not isinstance(analysis_type,str) or analysis_type.strip().casefold() not in ('smc','smc intraday')):
        raise TelegramError('A complete response, chart symbol and SMC analysis type are required.')
    header='<b>Trade With AM — SMC Trading Agent</b>\n'+html.escape(symbol)+' · '+html.escape(analysis_type.strip())
    return _split_formatted_response(_format_complete_markdown(response_text),header)


def format_simple_smc_message(report,symbol):
    if report.get('cli_completed') is not True or report.get('categories') != ['smc']:
        raise TelegramError('No completed SMC CLI analysis is available for Telegram.')
    return format_smc_response(report.get('final_analysis'),symbol,report.get('analysis_type','SMC Intraday'))


def _request(config, message, timeout_seconds, connection_factory=None):
    """Adapted bounded HTTPS POST from the reference sender; never follows redirects."""
    connection = None
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError()
        if connection is not None:
            connection.timeout = budget
            if connection.sock is not None:
                connection.sock.settimeout(budget)
        return budget

    try:
        factory = connection_factory or http.client.HTTPSConnection
        connection = factory("api.telegram.org", timeout=remaining(), context=ssl.create_default_context())
        connection.set_debuglevel(0)
        payload = {"chat_id": config.chat_id, "text": message, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        connection.request("POST", "/bot" + config.bot_token + "/sendMessage", body=body,
                           headers={"Content-Type": "application/json", "Accept": "application/json"})
        remaining()
        response = connection.getresponse()
        chunks, size = [], 0
        while True:
            remaining()
            chunk = response.read1(min(4096, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise TelegramError("Telegram response exceeded the size limit; acceptance is unconfirmed. No retry.")
            chunks.append(chunk)
        remaining()
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise TelegramError("Telegram returned an invalid response; acceptance is unconfirmed. No retry.") from None
        if response.status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            reasons = {400:"Telegram rejected the message or destination.", 401:"Telegram rejected the bot credentials.",
                       403:"Telegram denied access; check bot membership and permissions.", 429:"Telegram rate limited the request."}
            raise TelegramError(reasons.get(response.status, "Telegram did not confirm message acceptance.") + " No automatic retry.")
        result = data.get("result")
        expected_text = html.unescape(re.sub(r"</?(?:b|i)>", "", message))
        if (not isinstance(result, dict) or type(result.get("message_id")) is not int or result["message_id"] <= 0
                or not isinstance(result.get("chat"), dict) or type(result["chat"].get("id")) is not int
                or str(result["chat"]["id"]) != config.chat_id or result.get("text") != expected_text):
            raise TelegramError("Telegram acknowledgement did not match the configured destination/message; delivery is unconfirmed. No retry.")
        return result["message_id"]
    except TelegramError:
        raise
    except (TimeoutError, socket.timeout):
        raise TelegramError("Telegram network timeout; acceptance is unknown. Inspect Telegram before any manual resend. No automatic retry.") from None
    except Exception:
        # Never expose service text, token-bearing paths or raw transport errors.
        raise TelegramError("Telegram network/TLS connection failed; acceptance is unconfirmed. No automatic retry.") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def send_trade_analysis(report, symbol, timeframes, *, chart_verified, live_send_enabled=False,
                        timestamp=None, config_path=None, timeout_seconds=10, connection_factory=None):
    """One attempt per numbered part; stop on first failure, never restart/resend."""
    accepted, messages = [], []
    try:
        if live_send_enabled is not True:
            raise TelegramError("Telegram delivery is disabled; an explicit authorized call is required.")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise TelegramError("Telegram timeout must be between zero and 30 seconds.")
        messages = format_trade_messages(report, symbol, timeframes, chart_verified=chart_verified, timestamp=timestamp)
        config = load_config(config_path)
        if any(config.bot_token in html.unescape(text) for text in messages):
            raise TelegramError("Telegram report contains credentials; delivery blocked.")
        for message in messages:
            message_id = _request(config, message, timeout_seconds, connection_factory)
            if message_id in accepted:
                raise TelegramError("Telegram repeated a previous acknowledgement; this part is unconfirmed. No retry.")
            accepted.append(message_id)
        return DeliveryOutcome("accepted", len(accepted), len(messages), tuple(accepted))
    except TelegramError as exc:
        return DeliveryOutcome("failed", len(accepted), len(messages), tuple(accepted), str(exc))
    except Exception:
        return DeliveryOutcome("failed", len(accepted), len(messages), tuple(accepted),
                               "Telegram delivery could not be confirmed. No automatic retry.")
