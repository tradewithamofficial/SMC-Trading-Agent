"""Reusable S/R requests and Codex execution. Importing this module does nothing."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from sr_grouping import (
    GROUP_NAME, SETUP_GROUP_NAMES, SETUP_COLORS, GroupingSession,
    TradeSetupSession, completion_instructions, target_identity,
    ELLIOTT_GROUP_NAME, ELLIOTT_COLOR,
    SCENARIO_GROUPS, SCENARIO_COLORS, RESERVED_GROUP_NAMES,
)


DEFAULT_SYMBOL = "BYBIT:XAUUSDT.P"
DEFAULT_MODEL = "gpt-6-astra"
TIMEFRAMES = ("1H", "4H", "1D", "15m", "5m")
PROJECT_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def prepare_frozen_runtime():
    """Call after Tk loads, before workers launch system-installed programs."""
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        import ctypes
        # PyInstaller's DLL directory must not leak into Node/Codex children.
        # Tk and our Python extensions are already loaded at this point.
        if not ctypes.windll.kernel32.SetDllDirectoryW(None):
            raise ctypes.WinError()


def connector_directory():
    return Path(os.environ.get("SR_TRADINGVIEW_MCP_DIR", Path.home() / "tradingview-mcp")).expanduser().resolve()


def node_executable():
    """Resolve Node even with Explorer's PATH, without changing system settings."""
    override = os.environ.get("SR_NODE_PATH")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"SR_NODE_PATH does not exist: {path}")
        return str(path)
    found = shutil.which("node.exe") or shutil.which("node")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / "nodejs" / "node.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


# Only these MCP capabilities are authorized for Codex's confirmed analysis stage.
# The app owns a separate, explicitly confirmed symbol-selection operation.
# In particular: no deletion, arbitrary JS/UI input, symbol/tab switching,
# indicator changes, trading, or general-purpose execution tools.
TRADINGVIEW_SR_TOOLS = (
    "chart_get_state", "tab_list", "pane_list", "chart_get_visible_range",
    "data_get_ohlcv", "chart_set_timeframe", "draw_shape", "draw_list",
    "draw_get_properties",
)


def support_resistance_mcp_arguments(*, read_only=False):
    """Invocation-only policy; callers use this only after explicit consent.

    Keep the CLI's noninteractive approval policy and filesystem sandbox intact.
    Authorize named MCP tools through Codex's supported per-tool settings.
    """
    tools = tuple(t for t in TRADINGVIEW_SR_TOOLS if not read_only or t not in ('draw_shape', 'chart_set_timeframe'))
    arguments = ["-c", "mcp_servers.tradingview.enabled_tools=" + json.dumps(tools)]
    for tool in tools:
        arguments += ["-c", f'mcp_servers.tradingview.tools.{tool}.approval_mode="approve"']
    return arguments


@dataclass
class AnalysisOutcome:
    status: str
    response: str = ""
    errors: str = ""
    return_code: int | None = None
    output: str = ""
    prompt: str = ""
    # A process exit code is never evidence that chart objects were created.
    drawings_verified: bool = False
    analysis_completed: bool = False
    final_analysis: str = ""
    trade_report: dict | None = None
    telegram_delivery: object | None = None


def sanitize_output(value):
    """Remove terminal escapes and common credential formats from displayed logs."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(
        r'''(?i)(["']?(?:[\w-]*(?:api[_-]?key|token|secret|password))["']?\s*[:=]\s*)["']?[^\s,"'}]+["']?''',
        r"\1[REDACTED]", text,
    )
    return text


def fallback_symbols():
    """User-configured choices, never presented as a retrieved watchlist."""
    configured = os.environ.get("SR_SYMBOLS", "").split(",")
    return list(dict.fromkeys([DEFAULT_SYMBOL] + [s.strip() for s in configured if s.strip()]))


def get_tradingview_watchlist(timeout=25):
    """Reuse the installed connector's CLI and its watchlist_get implementation."""
    try:
        connector = connector_directory()
        node = node_executable()
        launcher = connector / "src" / "cli" / "index.js"
        if not node or not launcher.is_file():
            raise FileNotFoundError("Existing TradingView connector CLI or Node.js was not found.")
        result = subprocess.run(
            [node, str(launcher), "watchlist", "get"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=connector, timeout=timeout, shell=False,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"Connector exited with code {result.returncode}.")
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ValueError("Connector did not return a successful watchlist response.")
        symbols = list(dict.fromkeys(
            row["symbol"].strip() for row in data.get("symbols", [])
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
            and re.fullmatch(r"[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+", row["symbol"].strip())
        ))
        if not symbols:
            raise ValueError("TradingView returned an empty watchlist.")
        return {"symbols": symbols, "source": "live", "name": data.get("list_name") or "current list", "error": ""}
    except subprocess.TimeoutExpired:
        error = f"Watchlist retrieval timed out after {timeout} seconds."
    except (OSError, ValueError, TypeError) as exc:
        error = sanitize_output(exc)
    return {"symbols": fallback_symbols(), "source": "fallback", "name": "", "error": error}


def load_codex_models(catalog_path=None):
    """Read visible CLI model IDs locally; listing is not proof of invocation access."""
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = Path(catalog_path) if catalog_path is not None else home / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        models = list(dict.fromkeys(
            item["slug"] for item in data.get("models", [])
            if isinstance(item, dict) and item.get("visibility") == "list"
            and item.get("supported_in_api") is not False
            and isinstance(item.get("slug"), str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+", item["slug"])
        ))
        if not models:
            raise ValueError("No visible models were found in the local Codex catalog.")
        return {"models": models, "source": str(path), "fetched_at": data.get("fetched_at", "unknown"), "error": ""}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {"models": [], "source": str(path), "error": sanitize_output(exc)}


def verify_tradingview_chart(symbol, timeout=15):
    """Strict read-only destination verification; never allows a mismatch."""
    return _read_tradingview_chart(symbol, timeout)


def inspect_tradingview_chart(timeout=15):
    """Read the exact current source chart for the GUI's switch confirmation."""
    return _read_tradingview_chart(None, timeout)


def _read_tradingview_chart(expected_symbol, timeout):
    """Read existing tab_list/chart_get_state/pane_list cores; never switch targets.

    Without reliable active-tab attribution, require exactly one chart tab and
    one identified active pane. Ambiguity stops execution rather than guessing.
    """
    connector = connector_directory()
    node = node_executable()
    launcher = connector / "src" / "cli" / "index.js"
    if not node or not launcher.is_file():
        raise ValueError("TradingView chart unavailable: existing connector CLI or Node.js was not found.")

    def read(*args):
        try:
            result = subprocess.run(
                [node, str(launcher), *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", cwd=connector, timeout=timeout,
                stdin=subprocess.DEVNULL,
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode:
                raise ValueError(result.stderr.strip() or f"Connector exited with code {result.returncode}.")
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or data.get("success") is not True:
                raise ValueError("Connector could not read the current chart.")
            return data
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ValueError("TradingView chart unavailable: " + sanitize_output(exc)) from None

    tabs = read("tab", "list").get("tabs")
    if not isinstance(tabs, list) or len(tabs) != 1 or not isinstance(tabs[0], dict):
        raise ValueError("TradingView chart unavailable or ambiguous. Use one dedicated chart tab and one pane, then try again.")
    tab = tabs[0]
    if tab.get("is_chart") is not True or not tab.get("id") or not tab.get("url"):
        raise ValueError("TradingView chart unavailable: no identifiable chart tab is open.")
    state = read("state")
    symbol = state.get("symbol")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+", symbol):
        raise ValueError("The current chart must expose its full exchange-qualified symbol.")
    if expected_symbol is not None and symbol != expected_symbol:
        raise ValueError(f"Symbol mismatch: selected {expected_symbol}; TradingView shows {symbol}. Analysis was not started.")
    if type(state.get("chartType")) is not int or state["chartType"] != 1:
        raise ValueError("TradingView must use standard Candles (chart type 1). Change the chart type yourself, then try again.")
    layout = read("pane", "list")
    panes = layout.get("panes")
    if layout.get("chart_count") != 1 or not isinstance(panes, list) or len(panes) != 1:
        raise ValueError("Cannot attribute one exact chart pane. Use a dedicated single-chart layout, then try again.")
    pane = panes[0]
    if (not isinstance(pane, dict) or type(pane.get("index")) is not int
            or layout.get("active_index") != pane["index"] or pane.get("symbol") != symbol):
        raise ValueError("Current TradingView pane identity is unavailable or mismatched. Codex was not started.")
    # Detect tab changes during these reads. Codex must recheck the same identity
    # and candlestick type before analysis and immediately before drawing.
    final_tabs = read("tab", "list").get("tabs")
    if final_tabs != tabs:
        # Titles can include changing quotes; compare stable fields only.
        if (not isinstance(final_tabs, list) or len(final_tabs) != 1
                or not isinstance(final_tabs[0], dict)
                or any(final_tabs[0].get(k) != tab.get(k) for k in ("id", "url", "is_chart", "chart_id"))):
            raise ValueError("TradingView target changed during verification. No analysis was started; confirm again.")
    identity = {
        "tab_id": tab["id"], "url": tab["url"], "chart_id": tab.get("chart_id"),
        "pane_index": pane["index"], "layout": layout.get("layout"),
        "symbol": symbol, "chart_type": 1,
    }
    return json.dumps(target_identity(identity), ensure_ascii=True)


# Application-owned Node program: call the EXISTING connector core/setter and
# wait helpers, not a new chart API implementation or model-supplied page JS.
# A single cached CDP connection is checked against the approved source tab ID.
_SELECT_SYMBOL_PROGRAM = r"""
import * as chart from './src/core/chart.js';
import * as tab from './src/core/tab.js';
import * as pane from './src/core/pane.js';
import {waitForChartRender} from './src/wait.js';
import {getTargetInfo, disconnect} from './src/connection.js';
let input = ''; for await (const chunk of process.stdin) input += chunk;
try {
 const p = JSON.parse(input), t = p.approved_target;
 if (!/^[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+$/.test(p.destination)) throw Error('Full destination symbol required');
 async function check(symbol) {
   const tabs = await tab.list(), state = await chart.getState(), panes = await pane.list();
   const connected = await getTargetInfo();
   const tabs2 = await tab.list();
   for (const list of [tabs, tabs2]) {
     if (list.tab_count !== 1 || list.tabs.length !== 1 || list.tabs[0].is_chart !== true
         || list.tabs[0].id !== t.tab_id || list.tabs[0].url !== t.url || list.tabs[0].chart_id !== t.chart_id)
       throw Error('Approved chart tab changed; stopped');
   }
   if (connected.id !== t.tab_id || connected.url !== t.url || state.symbol !== symbol || state.chartType !== 1
       || panes.layout !== t.layout || panes.chart_count !== 1 || panes.active_index !== t.pane_index
       || panes.panes.length !== 1 || panes.panes[0].index !== t.pane_index || panes.panes[0].symbol !== symbol)
     throw Error('Approved chart/pane/layout/symbol/candlestick identity mismatch');
 }
 await check(t.symbol);
 // The only mutation is this existing MCP-backed operation, once if needed.
 const changed = t.symbol === p.destination
   ? {success:true,symbol:p.destination,chart_ready:null}
   : await chart.setSymbol({symbol:p.destination});
 if (changed.success !== true || changed.symbol !== p.destination) throw Error('Connector rejected the requested symbol');
 // setSymbol's legacy header-text wait may return false even with a loaded exact
 // symbol. Use the existing canvas/spinner/stable-symbol render wait as well.
 const ready = await waitForChartRender();
 if (ready !== true) throw Error('Chart loading/render readiness timed out');
 await check(p.destination);
 console.log(JSON.stringify({success:true, symbol:p.destination, chart_ready:ready,
   connector_symbol_ready:changed.chart_ready}));
} catch (e) {
 console.log(JSON.stringify({success:false, error:e.message})); process.exitCode=1;
} finally { await disconnect(); }
"""


def ensure_selected_symbol(symbol, approved_chart, *, live_confirmed=False, timeout=35):
    """One explicitly approved symbol selection on the same dedicated chart.

    Existing strict verification stays unchanged for all analysis/drawing calls.
    No retries/recovery, aliases, tab/pane selection or model permissions added.
    """
    if not live_confirmed:
        raise ValueError("Explicit confirmation of this symbol selection is required.")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+", symbol):
        raise ValueError("Select a full exchange-qualified symbol; bare aliases are not accepted.")
    approved = target_identity(approved_chart)
    # Share the existing job lock: never switch while another app instance is
    # analyzing. The subsequent analysis takes this lock again and re-verifies.
    session = GroupingSession(approved, verify_tradingview_chart, connector_directory(), node_executable())
    with session.exclusive():
        if target_identity(inspect_tradingview_chart()) != approved:
            raise ValueError("TradingView changed since confirmation. Nothing was switched; confirm again.")
        connector, node = connector_directory(), node_executable()
        if not node or not (connector / "src" / "core" / "chart.js").is_file():
            raise ValueError("Existing TradingView connector or Node.js unavailable; no switch attempted.")
        try:
            result = subprocess.run(
                [node, "--input-type=module", "-e", _SELECT_SYMBOL_PROGRAM],
                input=json.dumps({"approved_target": approved, "destination": symbol}, ensure_ascii=True),
                capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=connector,
                timeout=timeout, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                data = json.loads(result.stdout)
            except ValueError:
                raise ValueError(result.stderr.strip() or "Existing connector returned no valid symbol-selection response.") from None
            if result.returncode or not isinstance(data, dict) or data.get("success") is not True:
                raise ValueError(data.get("error", "Existing connector symbol selection failed.") if isinstance(data, dict) else "Invalid connector response.")
            if data.get("symbol") != symbol or data.get("chart_ready") is not True:
                raise ValueError("Exact destination symbol and chart readiness were not confirmed.")
            verified = target_identity(verify_tradingview_chart(symbol))
            if verified != dict(approved, symbol=symbol):
                raise ValueError("An unexpected tab/chart/layout/pane changed during symbol selection.")
            return json.dumps(verified, ensure_ascii=True)
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Symbol selection stopped: " + sanitize_output(exc)
                             + " The symbol may have changed. Inspect TradingView; no automatic retry or recovery. Codex was not started.") from None


def build_all_charts_prompt(timeframes, levels_per_side=3, model=DEFAULT_MODEL):
    # Reuse existing input validation. No watchlist or analysis is executed here.
    build_support_resistance_prompt(DEFAULT_SYMBOL, timeframes, levels_per_side, model)
    return (
        "CONCEPT ONLY — ALL CHARTS IS NOT IMPLEMENTED BY THIS APPLICATION.\n\n"
        f"Requested model: {model}\nTimeframes: {', '.join(dict.fromkeys(timeframes))}\n"
        f"Future request: find {levels_per_side} support and {levels_per_side} resistance zones "
        "per chart in the currently selected TradingView watchlist, combining timeframe confluence.\n"
        "This is a conceptual request only, with no authorization to iterate charts, run analysis "
        "or change TradingView. A future implementation would need explicit approval and exact "
        "chart identity checks for every target. Do not execute this request.\n"
    )


def build_support_resistance_prompt(
    symbol, timeframes, levels_per_side=3, model=DEFAULT_MODEL, *,
    confirmed_chart="", live_confirmed=False,
):
    symbol = str(symbol).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+", symbol):
        raise ValueError("Enter an exchange-qualified TradingView symbol, e.g. BYBIT:XAUUSDT.P.")
    frames = list(dict.fromkeys(timeframes))
    if not frames or any(frame not in TIMEFRAMES for frame in frames):
        raise ValueError("Select at least one of 1H, 4H, 1D, 15m or 5m.")
    if isinstance(levels_per_side, bool) or not isinstance(levels_per_side, int) or not 1 <= levels_per_side <= 20:
        raise ValueError("Levels per side must be a whole number from 1 to 20.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(model)):
        raise ValueError("Enter a valid Codex model ID.")
    chart = str(confirmed_chart).strip()
    if live_confirmed and not chart:
        raise ValueError("An exact tab/layout/pane identity is required before live execution.")
    approval = (
        f"The user explicitly approved this single request. Automatically verified chart identity: {chart!r}.\n"
        "Read the actual chart identity and compare it with this approved target before any mutation. "
        "If the identity is unavailable, ambiguous, or mismatched, STOP without changes and explain.\n"
        if live_confirmed else
        "There is NO authorization for live chart changes yet. First use read-only MCP tools to report "
        "the exact tab, layout, active pane, exchange-qualified symbol and timeframe. Ask the user "
        "to explicitly confirm that exact identity and this drawing request. STOP and wait for "
        "their reply before changing timeframes, symbols, or drawings.\n"
    )
    return (
        "Trade With AM — Support & Resistance Agent\n\n"
        f"Requested model: {model}. This is a fresh request; do not assume shared chat/session state.\n"
        f"TradingView symbol: {symbol}\n"
        f"Analyze timeframes: {', '.join(frames)}\n"
        f"Find {levels_per_side} nearby support levels/areas and {levels_per_side} nearby resistance levels/areas "
        "TOTAL across the selected timeframes, combining confluence rather than multiplying the count per timeframe.\n\n"
        "Use only the existing configured TradingView MCP connector. The installed connector is at "
        f"{connector_directory()} and uses TradingView Desktop debugging port 9222. "
        "Do not install, rebuild or replace it. Do not run the debug launcher, close TradingView, "
        "use another data provider, or connect to the Bybit API. If MCP is unavailable, stop and report it.\n\n"
        + approval + "\n"
        "Before analysis or drawing, read the chart state and available tab/pane identity using read-only MCP tools. "
        f"Verify that the exact active target is {symbol}, including exchange and perpetual/contract suffix. "
        "Require standard Candles (chartType 1) and the same verified tab/pane identity; stop if unavailable or mismatched. "
        "Never switch to another symbol, tab, layout or pane to make it match. If more than one possible "
        "target exists and cannot be distinguished, stop. Recheck the same identity immediately before drawing.\n\n"
        "After explicit authorization, analyze visible price structure and available chart data on the selected "
        "timeframes. You may change timeframe only on the confirmed target, and restore its original timeframe "
        "when finished. Identify nearby meaningful levels or zones around current price; explain timeframe confluence "
        "and reasons briefly. Use actual data, never invent prices or force unsupported levels.\n"
        "Choose the drawing type for each level from market structure, repeated price reactions, swing levels "
        "and timeframe confluence. Use horizontal_line for a relatively precise support/resistance price. "
        "Use rectangle only for a genuine zone with meaningful lower and upper price boundaries. "
        "Do not force all levels to be zones or all levels to be lines, and do not force a mixture without evidence. "
        "Each line or zone counts as one level/area toward the requested total per side. "
        "Use red (#ff0000) for resistance and green (#008000) for support, applying connector-supported "
        "style overrides appropriate to each shape; match rectangle borders and fills where supported.\n"
        "Visible chart labels must contain ONLY prices and contributing timeframes. "
        "For a rectangle use 'LOW–HIGH | TF / TF'; for a horizontal line use 'PRICE | TF / TF'. "
        "Format examples only, not market levels: zone '4155.35–4161.06 | 1H / 4H / 1D'; "
        "line '4174.50 | 1H / 4H'. Use actual prices with instrument-appropriate precision and "
        "only selected timeframes that contribute to that level. Do not prefix visible labels with "
        "'Trade With AM', 'S1 Support', 'S2 Support', 'R1 Resistance', 'R2 Resistance', or any role/sequence label. "
        "Color distinguishes support from resistance. If label/style rendering is unsupported, report it "
        "without retrying or changing existing drawings.\n"
        "Preserve ownership information in documented separate internal identification or hidden metadata "
        "for newly created objects ONLY if the connector supports it. Do not invent metadata fields or tools. "
        "If hidden ownership metadata is unsupported, clearly report that limitation. Record returned new "
        "drawing IDs in the response with the verified target, support/resistance role, geometry and timeframes; "
        "these identify this run's new objects and are not proof of ownership of existing objects. "
        "Never infer ownership from color or simplified price/timeframe labels, and never automatically "
        "modify or delete ambiguously owned objects. The standalone application groups verified new drawings "
        "under 'Support and Resistance Levels' after analysis. Do not attempt grouping yourself or invent "
        "a tool or workaround that edits existing objects.\n"
        "Preserve all existing drawings and indicators. Never delete, replace, move or regroup existing objects. "
        "Do not place orders, change alerts, open trading panels, or process other charts/watchlist entries. "
        "Do not implement algorithms, agents, schedulers or write project files.\n"
        "Do not automatically retry a failed drawing call. On failure, stop and list any objects already created "
        "and any partial changes; do not clean them up. Read back the new drawing IDs/properties if supported. "
        "Report the verified target identity, chosen line/zone types and prices/bounds, evidence of created object IDs, grouping "
        "support, and any errors. Distinguish planned, attempted, confirmed and unverified drawings. "
        "A zero subprocess exit code is not proof of drawing success.\n"
    )


def codex_command():
    """Resolve the installed CLI without shell=True or executing an npm .cmd shim."""
    override = os.environ.get("SR_CODEX_PATH")
    found = override or shutil.which("codex.exe") or shutil.which("codex")
    if not found and os.name == "nt":
        # Explorer may not have picked up npm's user PATH entry yet.
        npm_shim = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "npm" / "codex.cmd"
        if npm_shim.is_file():
            found = str(npm_shim)
    if not found:
        raise FileNotFoundError("Codex CLI was not found. Add it to PATH or set SR_CODEX_PATH.")
    path = Path(found).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Codex CLI path does not exist: {path}")
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        path = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if not path.is_file():
            raise FileNotFoundError("Cannot resolve the npm Codex launcher. Set SR_CODEX_PATH to codex.exe.")
    if path.suffix.lower() in (".js", ".mjs"):
        if os.name == "nt":
            # Use the native binary so timeout terminates Codex itself, not just
            # its Node launcher while leaving an analysis child running.
            package = path.parent.parent
            binaries = list(package.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
            binaries += list(package.glob("vendor/*/bin/codex.exe"))
            if len(binaries) != 1:
                raise FileNotFoundError("Set SR_CODEX_PATH to the installed native codex.exe (could not resolve one unique binary).")
            return [str(binaries[0])]
        node = node_executable()
        if not node:
            raise FileNotFoundError("Node.js is required by this installed Codex launcher.")
        return [node, str(path)]
    return [str(path)]


def run_support_resistance_analysis(
    symbol, timeframes, levels_per_side=3, model=DEFAULT_MODEL, *,
    confirmed_chart="", live_confirmed=False, timeout=600,
):
    """Run once after caller obtains consent. No Tk dependency, retries or GUI side effects.

    Without live_confirmed=True and a chart identity, returns confirmation_required
    before launching any subprocess. The caller is responsible for obtaining consent.
    """
    try:
        prompt = build_support_resistance_prompt(
            symbol, timeframes, levels_per_side, model,
            confirmed_chart=confirmed_chart, live_confirmed=live_confirmed,
        )
    except (ValueError, TypeError) as exc:
        return AnalysisOutcome("invalid_input", errors=str(exc))
    if not live_confirmed:
        return AnalysisOutcome("confirmation_required", prompt=prompt,
                               errors="Explicit live-test approval and exact chart identity are required.")
    try:
        session = GroupingSession(confirmed_chart, verify_tradingview_chart, connector_directory(), node_executable())
        if session.target["symbol"] != symbol:
            raise ValueError("Approved chart symbol differs from the selected symbol.")
        with session.exclusive():
            try:
                session.prepare()
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                return AnalysisOutcome("grouping_preflight_failed", prompt=prompt, errors=sanitize_output(exc))
            prompt += completion_instructions(session)
            outcome = _execute_codex(prompt, model, timeout)
            if outcome.status == "completed_unverified":
                try:
                    path = session.finish(outcome.response, levels_per_side)
                    outcome.status = "completed_verified"
                    outcome.drawings_verified = True
                    outcome.output += f"\nApplication verified drawing properties and exact group membership. Ownership record: {path}"
                except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
                    outcome.status = "partial_success" if session.record["drawing_ids"] else "completed_unverified"
                    outcome.errors += "\nDrawing/group verification incomplete: " + sanitize_output(exc)
            if not outcome.drawings_verified:
                try:
                    session.save(outcome.status, error=outcome.errors)
                except OSError as exc:
                    outcome.errors += "\nCould not save ownership record: " + sanitize_output(exc)
                outcome.errors += "\nPartial chart changes may exist. Preserved without cleanup or automatic retry; inspect TradingView before another request."
            return outcome
    except (OSError, ValueError, TypeError) as exc:
        return AnalysisOutcome("grouping_preflight_failed", prompt=prompt, errors=sanitize_output(exc))


def elliott_direct_tool_guidance(*, read_only=False):
    """Static binding instructions reused from STEP 561C; no new tool layer."""
    text = """
APPLICATION DIRECT MCP POLICY (takes precedence over group-inspection requests):
The APPLICATION has already verified Object Tree groups/memberships with its fixed
helper and independently checks the entire chart afterward. Groups in the supplied
snapshot are application-verified context. Do not search for, inspect, create, move,
rename or delete groups yourself. Never use tool_search.tool_search_tool or search
for additional tools: that discovery route is unavailable in this CLI.

Invoke existing approved MCP bindings directly through functions.exec, for example:
text(await tools.mcp__tradingview__chart_get_state({}));
text(await tools.mcp__tradingview__tab_list({}));
text(await tools.mcp__tradingview__pane_list({}));
text(await tools.mcp__tradingview__chart_get_visible_range({}));
text(await tools.mcp__tradingview__draw_list({}));
text(await tools.mcp__tradingview__draw_get_properties({entity_id: "ACTUAL_ID"}));
draw_list returns shapes [{id,name}]. draw_get_properties requires entity_id and
returns actual points and the full properties object. Never guess an ID.
For full individual candles use tools.mcp__tradingview__data_get_ohlcv({count: 240, summary: false}).
For the selected timeframes only use tools.mcp__tradingview__chart_set_timeframe({timeframe: "D"});
pass D, 240, 60 or 15 only as required by the selected timeframes, then restore the
original resolution. This grants no symbol, tab, pane or layout switching.

Creation uses tools.mcp__tradingview__draw_shape(args) with these installed fields:
{shape: string, point: {time: number, price: number},
 point2?: {time: number, price: number}, overrides?: string, text?: string}.
overrides is a JSON STRING, not an object. point2 is only for two-point shapes.
draw_shape returns entity_id; read it with draw_get_properties({entity_id: ...}).
Use functions.exec only for the approved MCP calls. No imports, shell, filesystem
writes, arbitrary TradingView JavaScript, other servers/plugins, orders or Telegram.
If an approved binding/readback fails, STOP with FAILURE, no retry, cleanup or
fallback to discovery. Model output alone never proves chart success.
"""
    if read_only:
        return text.split('For the selected timeframes only', 1)[0] + (
            '\nREAD ONLY: no timeframe/symbol changes, drawing/group operations, shell, imports, arbitrary page JavaScript, '
            'Telegram or orders. Use only these inspection bindings. A failed binding is FAILURE, never WAIT.\n')
    return text


def build_trade_setup_prompt(
    symbol, timeframes=("1H", "4H", "1D"), model=DEFAULT_MODEL, *,
    confirmed_chart="", live_confirmed=False, elliott_wave=False, illustrative_test=False, category=None,
):
    """Existing setup request, optionally using the single Elliott test prompt."""
    if type(elliott_wave) is not bool:
        raise ValueError("Elliott test selection must be a boolean.")
    if type(illustrative_test) is not bool or illustrative_test and not elliott_wave:
        raise ValueError("Illustrative test requires explicit Elliott mode.")
    frames = list(dict.fromkeys(timeframes))
    build_support_resistance_prompt(symbol, frames, 1, model,
                                    confirmed_chart=confirmed_chart, live_confirmed=live_confirmed)
    consent = (
        f"Explicit approval covers this single conditional setup analysis and its verified new drawings/groups. Approved identity: {confirmed_chart}. "
        if live_confirmed else
        "NO live chart changes are authorized. Read and report exact chart identity, then STOP for explicit user confirmation. "
    )
    if category is not None:
        if category not in SCENARIO_GROUPS or not elliott_wave or illustrative_test:
            raise ValueError('Invalid Elliott scenario category.')
        return build_scenario_prompt(symbol, model, category, consent)
    if elliott_wave:
        prompt = (
            "Trade With AM — Elliott Wave & Fibonacci Analysis — single setup test\n"
            f"Requested Codex model: {model}\nSelected symbol: {symbol}\nAnalysis timeframes: {', '.join(frames)}\n"
            + consent + "\n"
            "Use only the existing configured TradingView MCP. Do not install or modify connectors/configuration. "
            "Before analysis, selected-timeframe changes and each drawing, verify the approved exchange-qualified symbol, "
            "tab ID, URL/chart ID, layout, active pane and standard Candles (chartType 1). Stop on ambiguity/mismatch. "
            "Never switch symbol, tab, layout or pane; restore the original timeframe when finished. "
            "Use the selected timeframes through the existing workflow only: no two-phase or Swing/Intraday scenario system.\n"
            "The application supplies its verified existing chart/group snapshot below. Inspect drawings with draw_list and "
            "draw_get_properties; do not search for group tools. Preserve ALL existing drawings, groups and studies. "
            "Do not adopt, move, delete, replace, update or regroup anything. Existing S/R and Price Action groups are read-only context. "
            "Create no separate S/R drawings or groups; sr_drawings MUST be [].\n"
            "Analyze Elliott Wave Theory with actual market structure, wave subdivisions/overlap and credible impulse/correction "
            "interpretations, supported by Fibonacci retracement and extension relationships. Use actual available timestamped "
            "OHLCV and anchors, not invented pivots or sampled aggregate extrema. When individual bars are needed, use the existing "
            "data_get_ohlcv tool in full-bar mode summary:false, with bounded history (maximum 500). If data/tool access fails or "
            "anchors cannot be verified, return status='incomplete' with errors; this is not a completed market WAIT.\n"
            "Identify at most ONE technically justified potential conditional trade setup. Do not force a setup, a wave count, "
            "a probability or unsupported targets. With adequate evidence but no credible setup, return decision='wait', setups=[], "
            "sr_drawings=[] and NO TRADE / WAIT with brief reasons; create no drawings or empty groups.\n"
            "For a valid setup, explain the current Elliott Wave interpretation/position, supporting Fibonacci anchors, ratios "
            "and actual price levels, expected price movement, LONG/SHORT direction, entry area, entry confirmation, invalidation, "
            "potential targets and cancellation conditions. Put wave/Fibonacci/path explanations in the existing reasoning and "
            "final_analysis fields; use strategy_type='Elliott Wave + Fibonacci'. Target 2 and approximate risk/reward remain "
            "optional and must be null when unsupported. Every trade idea is conditional on confirmation, never an order.\n"
            "Use the existing permitted draw_shape tool. Draw four to twelve objects TOTAL for the one setup. Required roles "
            "remain entry, confirmation, invalidation, target1; optional roles are target2, wave, fibonacci, projection. "
            "Use horizontal_line for important levels, rectangle for a genuine entry/target zone, and the supported two-point "
            "trend_line for labeled wave segments and projected directional paths. Each line/segment needs a readable actual "
            "text label; label wave counts, Fibonacci ratios/levels, confirmation and invalidation clearly. Do not request "
            "native Elliott/Fibonacci/arrow tools or invent unsupported APIs. If a path cannot be drawn/read back, disclose "
            "the limitation and do not claim it succeeded. Use only actual returned new IDs and point/property readbacks. "
            f"Use soft readable {ELLIOTT_COLOR} consistently (linecolor for lines/segments; matching color/backgroundColor for "
            "rectangles, with soft transparent fill). Do not recolor existing objects.\n"
            f"The APPLICATION alone verifies and creates exactly '{ELLIOTT_GROUP_NAME}' if a real setup is drawn. "
            "No other new groups, empty placeholders or additional scenarios. Do not create groups yourself, search for "
            "group-management tools, invoke ui_evaluate/arbitrary JavaScript, use shell workarounds or modify project files. "
            "An existing exact-name group requires operator review; do not overwrite, adopt or duplicate it. "
            "No automatic retry or cleanup after failure. No Telegram, exchange/account access, orders, alerts or trading panels.\n"
            + elliott_direct_tool_guidance()
        )
        if illustrative_test:
            prompt = prompt.replace("this single conditional setup analysis", "this single illustrative scenario test")
            prompt = prompt.replace(
                "Identify at most ONE technically justified potential conditional trade setup. Do not force a setup, a wave count, "
                "a probability or unsupported targets. With adequate evidence but no credible setup, return decision='wait', setups=[], "
                "sr_drawings=[] and NO TRADE / WAIT with brief reasons; create no drawings or empty groups.",
                "STEP 3B SINGLE-USE ILLUSTRATIVE TEST: identify and draw at most ONE defensible Elliott Wave/Fibonacci "
                "interpretation from actual chart evidence. An actionable trade entry is NOT required. Never invent a count, "
                "anchors, levels or probabilities. If no defensible interpretation exists after successful analysis, report "
                "the limitation with decision='wait', setups=[], sr_drawings=[] and NO TRADE / WAIT. Create nothing in that case; "
                "do not add a mechanics-only placeholder in this run.")
            prompt = prompt.replace(
                "For a valid setup, explain the current Elliott Wave interpretation/position, supporting Fibonacci anchors, ratios "
                "and actual price levels, expected price movement, LONG/SHORT direction, entry area, entry confirmation, invalidation, "
                "potential targets and cancellation conditions. Put wave/Fibonacci/path explanations in the existing reasoning and "
                "final_analysis fields; use strategy_type='Elliott Wave + Fibonacci'. Target 2 and approximate risk/reward remain "
                "optional and must be null when unsupported. Every trade idea is conditional on confirmation, never an order.",
                "Explain the current wave interpretation/position, actual supporting Fibonacci anchors, ratios and levels, "
                "expected movement and interpretation-cancellation conditions in reasoning/final_analysis. Draw clear wave "
                "labels and relevant Fibonacci levels; add a projection, confirmation and invalidation levels only where "
                "supported. Explain missing confirmation/invalidation/targets rather than fabricating them. Return "
                "decision='illustration', rank=1, result_kind='illustrative_scenario', actionable=false, direction=null, "
                "entry_area=null and risk_reward=null. Keep strategy_type='Elliott Wave + Fibonacci'; confirmation, "
                "invalidation, target1 and target2 are explanatory strings only when supported, otherwise null. "
                "The final explanation MUST include exactly NO TRADE / WAIT — TEST SCENARIO ONLY. This is an analytical "
                "drawing, not a validated trade entry or recommendation.")
            prompt = prompt.replace(
                "Draw four to twelve objects TOTAL for the one setup. Required roles "
                "remain entry, confirmation, invalidation, target1; optional roles are target2, wave, fibonacci, projection.",
                "Draw two to twelve real objects TOTAL for the one illustration. Required drawing roles are wave and "
                "fibonacci; optional roles are projection, confirmation, invalidation, target1, target2. Do not create an entry drawing.")
            prompt = prompt.replace("if a real setup is drawn", "if an illustrative scenario is drawn")
        return prompt
    return (
        "Trade With AM — Conditional Price Action Trade Setups\n"
        f"Requested Codex model: {model}\nSelected symbol: {symbol}\nAnalysis timeframes: {', '.join(frames)}\n"
        + consent + "\n"
        "Use only the existing configured TradingView MCP. Do not install or modify connectors/configuration. "
        "Before analysis, timeframe changes and each drawing, verify the exact exchange-qualified selected symbol, "
        "tab ID, URL/chart ID, layout and active pane against the approved identity, requiring standard Candles (chartType 1). "
        "Stop on ambiguity/mismatch. Never switch symbol, tab, layout or pane. Change only selected analysis timeframes "
        "on that target, restoring its original timeframe when finished. No other charts or watchlist iteration.\n"
        f"Inspect existing drawings and the Object Tree group '{GROUP_NAME}'. The application supplies a verified "
        "group/membership snapshot below; compare relevant IDs and properties using draw_list/draw_get_properties. "
        "Consider existing S/R only if still relevant to current evidence. Explain which outdated levels need refreshing "
        "and why; never overwrite or silently refresh them. Do not infer ownership from labels/colors. "
        "If S/R is absent, identify relevant levels from current chart evidence. Only when the supplied snapshot "
        "has no S/R group and no conflicting ownership, you may create up to three evidence-backed supports and three "
        "resistances as new lines/rectangles, recording them in sr_drawings. The application verifies/groups them. "
        "If safety or evidence is insufficient, create no S/R drawings and explain what is missing. "
        "Do not adopt, move, delete, replace, regroup or modify ANY existing drawing, indicator or group.\n"
        "Analyze market trend and structure, higher highs / higher lows, lower highs / lower lows, breakout and retest, "
        "trend continuation, pullbacks to support/resistance, technically justified failed breakouts, and relevant "
        "candlestick confirmation. Use actual current chart data and selected timeframe confluence; never invent prices. "
        "Identify up to THREE worthwhile CONDITIONAL setups, ranked by relative technical strength/confluence: "
        "Setup 1 — Primary; Setup 2 — Secondary; Setup 3 — Alternative. Do not fabricate numerical success probabilities "
        "or claim a trade will succeed. Return fewer than three when warranted; if none are attractive, return "
        "NO TRADE / WAIT with concrete reasons and no placeholder setups/drawings/groups.\n"
        "For every valid setup explain LONG or SHORT, strategy type, market structure reasoning, entry area, required "
        "price-action confirmation, invalidation / protective stop concept, Target 1, Target 2 only when justified, "
        "approximate risk/reward only when supportable (explain assumptions; otherwise unavailable), and conditions "
        "that cancel the idea. Confirmation must occur before considering an entry; all ideas remain conditional.\n"
        "For each valid setup draw real entry areas/levels, confirmation triggers, invalidation levels and target levels "
        "using the verified horizontal_line and rectangle shapes with short explanatory labels. Represent direction "
        "in labels; do not probe unsupported directional arrows. Each setup needs four to eight drawings including "
        "roles entry, confirmation, invalidation, target1; target2 is optional. Use consistent distinct scenario colors: "
        f"Setup 1 {SETUP_COLORS[0]}, Setup 2 {SETUP_COLORS[1]}, Setup 3 {SETUP_COLORS[2]}. "
        "Match rectangle borders/fills where supported. Any optional new S/R uses green #008000 for support and red "
        "#ff0000 for resistance, with price/timeframe-only labels. Record returned new IDs and actual point/property "
        "readbacks; never claim an unverified object succeeded. No automatic retry or cleanup after a partial failure.\n"
        f"The application alone creates independent Object Tree groups: {', '.join(SETUP_GROUP_NAMES)}. "
        "Do not group drawings yourself or request ui_evaluate/arbitrary JavaScript, UI automation or shell workarounds. "
        "An existing setup group requires ownership inspection and operator review; do not overwrite or duplicate it.\n"
        "No Bybit/exchange/trading API integration, order placement, live account actions, automatic trade execution, "
        "Telegram, alerts, trading panels, account access, general-purpose execution or project file changes. "
        "This request is conditional chart analysis and annotations only.\n"
    )


def _trade_completion_instructions(session):
    if session.category is not None:
        context = {'target':session.target, 'resolution':session.before['resolution'], 'groups':session.before['groups'],
            'drawings':[{'id':d['id'],'shape':d['shape'],'points':d['points'],'text':d['properties'].get('text','')}
                        for d in session.before['drawings']]}
        return ('\nAPPLICATION-VERIFIED READ-ONLY CONTEXT (all full properties preserved independently):\n' + json.dumps(context)
            + '\nNewly verified Swing analysis, if completed in this run:\n' + json.dumps(session.swing_context)
            + '\nFINAL MESSAGE: ONLY schema 1 JSON with run_id=' + json.dumps(session.run_id) + ', target='
            + json.dumps(session.target) + ", status='complete' only after successful data/analysis/drawing readbacks and "
            "original-timeframe restoration; errors=[]; otherwise status='incomplete' with actual errors. "
            "decision='scenarios' for 1–3 scenarios or 'wait' for none; final_analysis=human-readable analysis including "
            "NO TRADE / WAIT if no attractive entry; setups=array; sr_drawings=[]. Each item requires rank (consecutive 1–3), "
            "category=" + json.dumps(session.category) + ", result_kind='scenario', strategy_type, reasoning (wave interpretation/position), "
            "expected_path, fibonacci_context (actual timestamp/price anchors, ratios and levels), confirmation, invalidation, "
            "watch_next, cancel_conditions (meaningful nonempty strings); target1/target2 (supported strings or null); "
            "direction LONG/SHORT, entry_area and conditional_trade_idea ONLY if justified, otherwise ALL null; risk_reward null "
            "unless supportable. Intraday also requires relationship compatible|neutral|corrective|conflicting, swing_relationship "
            "(explanation), related_swing_groups (array of actual existing Swing group names, or [] for HTF-only context). "
            "Each scenario has 2–12 actual drawings with unique returned IDs; required roles wave and fibonacci; optional "
            "projection, confirmation, invalidation, entry (only for a real conditional idea), target1, target2. "
            "Each drawing: id, role, shape horizontal_line|trend_line|rectangle, actual points [{time,price}], properties "
            "(actual readback subset including text and linecolor, or text/color/backgroundColor for rectangles). "
            "Restore original timeframe BEFORE final point/property readbacks. Do not claim grouping/Telegram success.\n")
    sr_groups = [g for g in session.before["groups"] if g["name"] == GROUP_NAME]
    member_ids = {eid for g in sr_groups for eid in g["ids"]}
    context = {"groups": sr_groups, "drawings": [d for d in session.before["drawings"] if d["id"] in member_ids]}
    context = session.before if session.elliott_wave else context
    instructions = (
        "\nAPPLICATION-VERIFIED S/R GROUP CONTEXT (read-only; not permission to adopt objects):\n"
        + json.dumps(context, ensure_ascii=True) + "\nFINAL-MESSAGE PROTOCOL\n"
        "Return ONLY a JSON object, without Markdown fences. Required fields: schema=1, "
        f"run_id={json.dumps(session.run_id)}, target={json.dumps(session.target)}, "
        "status='complete' only for a completed analysis with all requested readbacks and restored timeframe, "
        "otherwise 'incomplete'; errors (array), decision ('setups' or 'wait'), final_analysis (string), "
        "setups (array of zero to three), sr_drawings (array, empty unless safe new S/R was drawn). "
        "final_analysis must contain ONLY your complete final human-readable conditional setup explanations, "
        "including rankings, all requested fields and S/R refresh/missing-level observations, or NO TRADE / WAIT "
        "with reasons. Exclude prompts, raw JSON, MCP calls, execution traces, diagnostics and success claims "
        "about grouping, which the application has not yet performed.\n"
        "Each setup object requires rank (consecutive integer from 1), direction ('LONG'/'SHORT'), strategy_type, "
        "reasoning, entry_area, confirmation, invalidation, target1, cancel_conditions (nonempty strings), "
        "target2 and risk_reward (explanatory strings when supported, otherwise null), drawings (array). "
        "Each drawing requires id (actual returned new ID), role, shape ('horizontal_line'/'rectangle'), "
        "points (actual readback [{time,price}]), properties (actual readback subset including text and linecolor "
        "for lines, text/color/backgroundColor for rectangles). Setup roles: entry, confirmation, invalidation, "
        "target1, optional target2. sr_drawings roles: support/resistance. Do not omit or reuse drawing IDs.\n"
    )
    if session.elliott_wave:
        instructions = instructions.replace("APPLICATION-VERIFIED S/R GROUP CONTEXT", "APPLICATION-VERIFIED CHART/GROUP CONTEXT")
        instructions = instructions.replace("zero to three", "zero or one")
        instructions = instructions.replace("array, empty unless safe new S/R was drawn", "array, MUST be empty for this test")
        instructions = instructions.replace("rankings, all requested fields and S/R refresh/missing-level observations",
                                            "the wave interpretation, Fibonacci anchors/levels and all requested fields")
        instructions = instructions.replace("'horizontal_line'/'rectangle'", "'horizontal_line'/'rectangle'/'trend_line'")
        if session.illustrative_test:
            instructions = instructions.replace("decision ('setups' or 'wait')", "decision ('illustration' or 'wait')")
            instructions = instructions.replace("your complete final human-readable conditional setup explanations",
                                                "your complete final human-readable test-only wave/Fibonacci interpretation")
            instructions = instructions.replace(
                "Each setup object requires rank (consecutive integer from 1), direction ('LONG'/'SHORT'), strategy_type, "
                "reasoning, entry_area, confirmation, invalidation, target1, cancel_conditions (nonempty strings), "
                "target2 and risk_reward (explanatory strings when supported, otherwise null), drawings (array).",
                "The sole illustrative item in setups requires rank=1, result_kind='illustrative_scenario', actionable=false, "
                "direction=null, entry_area=null, risk_reward=null; strategy_type, reasoning, cancel_conditions (nonempty strings); "
                "confirmation, invalidation, target1 and target2 (supported explanatory strings, otherwise null); drawings (array). "
                "Use decision='illustration' for this item, or 'wait' for no item. final_analysis must include "
                "NO TRADE / WAIT — TEST SCENARIO ONLY for a drawn illustration.")
            instructions = instructions.replace(
                "Setup roles: entry, confirmation, invalidation, target1, optional target2.",
                "Illustration roles: required wave and fibonacci; optional projection, confirmation, invalidation, target1, target2. No entry role.")
            instructions += "\nTwo to twelve real drawings for the sole illustrative item; no placeholders or model grouping.\n"
        else:
            instructions += ("\nSINGLE ELLIOTT MODE: reuse exactly the above schema/fields. Four to twelve drawings for the sole rank 1 setup; "
                "wave, fibonacci and projection are optional drawing roles in addition to the required entry/confirmation/invalidation/target1. "
                "A trend_line requires two actual readback points, text and linecolor. No sr_drawings or model grouping.\n")
    return instructions


def run_trade_setup_analysis(
    symbol, timeframes=("1H", "4H", "1D"), model=DEFAULT_MODEL, *,
    confirmed_chart="", live_confirmed=False, timeout=600, elliott_wave=False, illustrative_test=False, category=None, swing_context=None,
):
    """One confirmed setup job using the existing CLI/lock/connector."""
    try:
        prompt = build_trade_setup_prompt(symbol, timeframes, model,
                                         confirmed_chart=confirmed_chart, live_confirmed=live_confirmed, elliott_wave=elliott_wave,
                                         illustrative_test=illustrative_test, category=category)
    except (ValueError, TypeError) as exc:
        return AnalysisOutcome("invalid_input", errors=str(exc))
    if not live_confirmed:
        return AnalysisOutcome("confirmation_required", prompt=prompt, errors="Explicit confirmation and exact chart identity are required.")
    try:
        session = TradeSetupSession(confirmed_chart, verify_tradingview_chart, connector_directory(), node_executable())
        session.elliott_wave = elliott_wave is True
        session.illustrative_test = illustrative_test
        session.category = category
        session.swing_context = swing_context
        if session.target["symbol"] != symbol:
            raise ValueError("Approved chart symbol differs from the selected symbol.")
        with session.exclusive():
            try:
                session.prepare()
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                return AnalysisOutcome("grouping_preflight_failed", prompt=prompt, errors=sanitize_output(exc))
            prompt += _trade_completion_instructions(session)
            options = {"elliott_wave": True} if elliott_wave else {}
            outcome = _execute_codex(prompt, model, timeout, **options)
            if outcome.status == "completed_unverified":
                try:
                    if category is not None:
                        _verify_phase_data(outcome.output, category, symbol)
                    count = session.finish(outcome.response)
                    outcome.status = "completed_verified" if count else "completed_no_trade"
                    outcome.drawings_verified = True
                    kind = "illustrative scenario" if illustrative_test else "setup"
                    outcome.output += f"\nApplication verified {count} {kind} groups and chart preservation. Ownership record: {session.path}"
                except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
                    outcome.status = "partial_success" if session.record["drawing_ids"] else "completed_unverified"
                    outcome.errors += "\nTrade setup drawing/group verification incomplete: " + sanitize_output(exc)
                outcome.analysis_completed = getattr(session, "analysis_completed", False)
                outcome.final_analysis = sanitize_output(getattr(session, "final_analysis", ""))
                outcome.trade_report = getattr(session, "completed_report", None)
            if not outcome.drawings_verified:
                outcome.errors += "\nPartial changes may exist. No automatic retry or cleanup; inspect TradingView before another request."
                try:
                    session.save(outcome.status, error=outcome.errors)
                except OSError as exc:
                    outcome.errors += "\nCould not save ownership record: " + sanitize_output(exc)
            return outcome
    except (OSError, ValueError, TypeError) as exc:
        return AnalysisOutcome("grouping_preflight_failed", prompt=prompt, errors=sanitize_output(exc))


def codex_failure(response, output, errors):
    """STEP 561B guard: runtime/tool failures cannot become a successful WAIT."""
    for line in errors.splitlines():
        if re.match(r"^(?:\d{4}-\S+\s+)?(?:ERROR\b|Error:)", line):
            return "Codex infrastructure error: " + sanitize_output(line)[:800]
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in ("error", "turn.failed"):
            return "Codex execution reported FAILURE: " + sanitize_output(line)[:800]
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") in ("mcp_tool_call", "tool_call", "command_execution"):
            result = item.get("result")
            if (item.get("status") == "failed" or item.get("error")
                    or isinstance(result, dict) and result.get("isError") is True):
                return "Codex tool call reported FAILURE: " + sanitize_output(line)[:800]
            if isinstance(result, dict):
                for block in result.get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    try:
                        payload = json.loads(block.get("text", ""))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(payload, dict) and payload.get("success") is False:
                        return "Codex tool returned an unsuccessful result. FAILURE; not a market WAIT."
    if re.search(r"unsupported call:|tool discovery failed|(?:MCP|TradingView) (?:connection|tool call) failed", response, re.I):
        return "Codex could not complete live tool access. FAILURE; not a market WAIT."
    try:
        answer = json.loads(response)
    except (ValueError, TypeError):
        answer = None
    if (isinstance(answer, dict) and answer.get("status") in ("failed", "error", "infrastructure_failed")
            or re.match(r"\s*FAILURE\b", response)):
        return "Codex reported FAILURE; not a market WAIT."
    return None


def build_scenario_prompt(symbol, model, category, consent):
    order = 'Daily D (1D), then 240 (4H), then 60 (1H)' if category == 'swing' else '15m (resolution 15)'
    return (f'Trade With AM — Elliott Wave & Fibonacci Trading Agent\nModel: {model}\nSymbol: {symbol}\nCATEGORY: {category}\n'
        + consent + '\nAnalyze ' + order + '. Verify exact approved tab/URL/chart/layout/pane/symbol and standard Candles '
        'before each timeframe change/drawing. Read chart_get_state after each change; D and 1D are Daily only when actual '
        'resolution confirms Daily. Restore the original resolution before final drawing readbacks.\n'
        'Develop the THREE most defensible DISTINCT alternative Elliott Wave paths where actual evidence supports them: '
        '1 Primary, 2 Secondary, 3 Alternative. Three is a normal target, never a quota. Return fewer or zero if evidence '
        'does not support three. Do not invent counts, anchors, price levels, targets or probability percentages. '
        'Scenarios are analytical paths, NOT trade setups: draw plausible scenarios even when no attractive entry exists, '
        'then report NO TRADE / WAIT. A conditional LONG/SHORT idea is optional, never forced.\n'
        'Use actual timestamped OHLCV, Elliott impulse/correction rules, subdivisions, overlap/competing evidence, '
        'Fibonacci retracements/extensions/projections from actual anchors, market structure, support/resistance and reaction zones. '
        'Request full bars with summary:false and bounded count (normally 240, maximum 500); never treat five sampled summary '
        'candles as sufficient. Insufficient/malformed history or failed tools are incomplete FAILURE, never successful WAIT. '
        'Treat forming candles as unconfirmed. Explain current wave position, expected next path, Fib anchors/levels, '
        'confirmation, weakening/invalidation, supported target/reaction zones, cancellation and what to watch next.\n'
        + ('Complete this Swing phase first; Intraday is a separate subsequent call using its verified result.\n' if category == 'swing' else
           'Use the newly verified Swing analysis supplied below when available. Otherwise inspect supplied existing Swing groups/labels '
           'as context, not guaranteed current analysis. If no Swing groups exist, inspect D/240/60 structure as necessary before 15m, '
           'but create NO Swing drawings/groups. For each Intraday scenario explain compatible, neutral, corrective or conflicting '
           'relationships, naming actual available Swing groups. A bullish short rally can be corrective within bearish Swing structure.\n')
        + 'Use existing horizontal_line, trend_line and rectangle tools only. Draw a compact wave count/labels, supported directional '
        'projection, relevant Fib levels, confirmation/invalidation levels and meaningful zones/targets. Disclose unsupported/uncertain '
        'projections rather than inventing them. Each scenario has 2–12 real drawings with independent new IDs, exclusively its own group. '
        'Use readable annotations including "Confirmation: ..." and "Invalidation: ..." with actual conditions/timeframes, '
        'so a later inquiry can read the scenario without a saved manifest. Keep visual clutter low. '
        f'Use these exact soft colors by rank: {json.dumps(SCENARIO_COLORS[category])}; linecolor for lines and color/backgroundColor for zones. '
        f'The APPLICATION alone will verify/create {json.dumps(SCENARIO_GROUPS[category])}, only for qualifying ranks. '
        'Do not create empty groups. All pre-existing drawings/groups/studies are immutable context. Never delete, update, rename, '
        'move or regroup them. The application already handled any explicitly confirmed selected-category removal before this call. '
        'Do not inspect/search group-management tools, use arbitrary page JS, shell, other charts, orders or Telegram. '
        'Any failure stops the run: no retry, cleanup or rollback.\n' + elliott_direct_tool_guidance())


def _same_snapshot(left, right):
    def canonical(value):
        return dict(value, drawings=sorted(value['drawings'], key=lambda d:d['id']),
                    groups=sorted([dict(g, ids=sorted(g['ids'])) for g in value['groups']], key=lambda g:g['id']))
    return canonical(left) == canonical(right)


def _validate_bars(bars, minimum):
    if not isinstance(bars, list) or len(bars) < minimum:
        raise ValueError('INSUFFICIENT MARKET DATA: full timestamped candles required.')
    times = []
    for bar in bars:
        if not isinstance(bar,dict) or any(type(bar.get(k)) not in (int,float) or not math.isfinite(bar[k]) for k in ('time','open','high','low','close')):
            raise ValueError('Malformed full market candles.')
        if not bar['low'] <= min(bar['open'],bar['close']) <= max(bar['open'],bar['close']) <= bar['high']:
            raise ValueError('Invalid OHLC price range.')
        times.append(bar['time'])
    if times != sorted(set(times)): raise ValueError('Candle timestamps are not strictly increasing.')


def _verify_phase_data(output, category, symbol):
    resolution = None;seen = []
    aliases = {'D':'1D','1D':'1D','240':'4H','60':'1H','15':'15m'}
    for call in _logged_mcp_calls(output):
        if call.get('server') != 'tradingview' or call.get('tool') not in TRADINGVIEW_SR_TOOLS or call.get('status') != 'completed':
            raise ValueError('Unexpected/failed analysis tool route.')
        payload = _tool_result(call)
        if payload.get('success') is False:
            raise ValueError('Market/drawing tool returned failure.')
        if call['tool'] == 'chart_set_timeframe': resolution = None
        if call['tool'] == 'chart_get_state':
            if payload.get('symbol') != symbol: raise ValueError('Market data symbol mismatch.')
            resolution = aliases.get(str(payload.get('resolution')))
        if call['tool'] != 'data_get_ohlcv': continue
        bars = payload.get('bars')
        if call.get('arguments',{}).get('summary') is not False or resolution is None or not isinstance(bars,list) or len(bars) < 30:
            raise ValueError('INSUFFICIENT MARKET DATA: full timestamped candles and verified resolution required.')
        _validate_bars(bars, 30)
        if not seen or seen[-1] != resolution: seen.append(resolution)
    if (category == 'swing' and seen != ['1D','4H','1H']) or (category == 'intraday' and '15m' not in seen):
        raise ValueError('Required ordered timeframe/full-candle analysis evidence missing.')


def _validate_removal(inspection, categories):
    snapshot, scopes = inspection['snapshot'], inspection['groups']
    names = [name for category in categories for name in SCENARIO_GROUPS[category]]
    matching = [g for g in snapshot['groups'] if g['name'] in names]
    if len({g['name'] for g in matching}) != len(matching) or len(scopes) != len(matching):
        raise ValueError('Duplicate or missing selected scenario group scope; operator review required.')
    drawings = {d['id'] for d in snapshot['drawings']}
    for scope in scopes:
        if (scope.get('name') not in names or scope.get('subgroups') != [] or scope.get('parents') != [] or not isinstance(scope.get('dependencies'), list)
                or sorted(d['id'] for d in scope['dependencies']) != sorted(scope['ids'])
                or any('link_key' not in d or d['link_key'] is not None for d in scope['dependencies'])
                or len(set(scope['ids'])) != len(scope['ids']) or set(scope['ids']) - drawings
                or any(set(scope['ids']) & set(g['ids']) for g in snapshot['groups'] if g['id'] != scope['id'])):
            raise ValueError('Nested, linked, shared or unknown deletion scope; operator review required. Nothing authorized for deletion.')
        if {'id':scope['id'], 'name':scope['name'], 'ids':scope['ids']} not in matching:
            raise ValueError('Group membership changed; operator review required.')


def run_elliott_analysis(symbol, categories, model=DEFAULT_MODEL, *, confirmed_chart='', live_confirmed=False,
                         replacement_approval=None, allow_replacement=True, timeout=600):
    try:
        return _run_elliott_analysis(symbol, categories, model, confirmed_chart=confirmed_chart,
            live_confirmed=live_confirmed, replacement_approval=replacement_approval,
            allow_replacement=allow_replacement, timeout=timeout)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
        return AnalysisOutcome('failed', errors=sanitize_output(exc) + '\nInspect chart; no restoration retry or cleanup.')


def _run_elliott_analysis(symbol, categories, model=DEFAULT_MODEL, *, confirmed_chart='', live_confirmed=False,
                          replacement_approval=None, allow_replacement=True, timeout=600):
    """Small sequential adapter around the proven setup executor, no new engine."""
    session = initial = None
    removed = set()
    removed_groups = set()
    try:
        if (not isinstance(categories, (list, tuple)) or not categories or len(set(categories)) != len(categories)
                or any(c not in SCENARIO_GROUPS for c in categories)):
            raise ValueError('Select Swing, Intraday or both.')
        categories = [c for c in SCENARIO_GROUPS if c in categories]
        for c in categories:
            build_trade_setup_prompt(symbol, ['1D','4H','1H'] if c == 'swing' else ['15m'], model,
                confirmed_chart=confirmed_chart, live_confirmed=live_confirmed, elliott_wave=True, category=c)
        if not live_confirmed:
            return AnalysisOutcome('confirmation_required', errors='Explicit chart/analysis confirmation required.')
        session = GroupingSession(confirmed_chart, verify_tradingview_chart, connector_directory(), node_executable())
        if session.target['symbol'] != symbol:
            raise ValueError('Selected symbol differs from approved chart.')
        with session.exclusive():
            inspection = session.call('inspect_reserved', {'categories':categories})
            session._snapshot(inspection['snapshot']);_validate_removal(inspection, categories)
            initial = inspection['snapshot']
            if replacement_approval is not None and replacement_approval != inspection:
                raise ValueError('Chart/group/dependency scope changed since removal confirmation. Confirm a new request.')
            if inspection['groups'] and not allow_replacement:
                return AnalysisOutcome('replacement_disabled', errors='Creation-only test: remove old selected groups manually or separately authorize replacement.')
            if inspection['groups'] and replacement_approval is None:
                return AnalysisOutcome('replacement_confirmation_required', trade_report={'replacement':inspection},
                    errors='Old selected scenarios are removed BEFORE analysis; subsequent failure may leave them absent or partial. No rollback/retry.')
            current = initial
            for scope in inspection['groups']:
                session.save('removal_attempt', before_snapshot=current, scope=scope)
                receipt = session.call('remove_reserved', {'categories':categories, 'scope':scope, 'snapshot':current})
                independent = session.call('snapshot')
                session._snapshot(independent)
                if not _same_snapshot(independent, receipt['after']):
                    raise ValueError('Post-deletion verification failed; inspect partial changes. No retry.')
                current = independent;removed.update(scope['ids']);removed_groups.add(scope['id'])
                session.save('removed_verified', after_snapshot=current, removed_ids=sorted(removed))
        phases = {};responses = [];new_ids = set()
        for category in categories:
            phase = run_trade_setup_analysis(symbol, ['1D','4H','1H'] if category == 'swing' else ['15m'], model,
                confirmed_chart=confirmed_chart, live_confirmed=True, timeout=timeout, elliott_wave=True,
                category=category, swing_context=phases.get('swing'))
            if not phase.drawings_verified or phase.status not in ('completed_verified','completed_no_trade'):
                phase.status = 'failed';phase.drawings_verified = False;phase.trade_report = None
                phase.errors = category.title() + ' phase failed; remaining categories stopped. ' + phase.errors + (
                    '\nOld selected scenarios may already be absent; new partial drawings are left for review. No retry or cleanup.')
                return phase
            phases[category] = phase.trade_report;responses.append(phase.final_analysis)
            new_ids.update(d['id'] for s in json.loads(phase.response)['setups'] for d in s['drawings'])
        after = session.call('snapshot');session._snapshot(after)
        kept = {d['id']:d for d in initial['drawings'] if d['id'] not in removed}
        actual = {d['id']:d for d in after['drawings']}
        expected_names = {SCENARIO_GROUPS[c][i] for c,r in phases.items() for i in range(r['scenario_count'])}
        retained_groups = [g for g in initial['groups'] if g['name'] not in {n for c in categories for n in SCENARIO_GROUPS[c]}]
        additions = [g for g in after['groups'] if g['id'] not in {x['id'] for x in retained_groups}]
        if (after['resolution'] != initial['resolution'] or after['studies'] != initial['studies']
                or set(actual) != set(kept) | new_ids or any(actual.get(k) != v for k,v in kept.items())
                or any(g not in after['groups'] for g in retained_groups)
                or len(additions) != len(expected_names) or {g['name'] for g in additions} != expected_names
                or set(eid for g in additions for eid in g['ids']) != new_ids):
            raise ValueError('Final selected-category/unrelated-object verification failed; inspect chart. No cleanup.')
        report = {'analysis_kind':'elliott_wave', 'run_id':session.run_id, 'categories':categories, 'phases':phases,
            'setups':[s for p in phases.values() for s in p['setups'] if s['direction'] is not None],
            'final_analysis':'\n\n'.join(responses)}
        session.save('verified', groups=additions, after_snapshot=after,
            scenario_counts={c:p['scenario_count'] for c,p in phases.items()})
        return AnalysisOutcome('completed_verified', analysis_completed=True, drawings_verified=True,
            final_analysis=report['final_analysis'], trade_report=report, return_code=0)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
        return AnalysisOutcome('failed', errors=sanitize_output(exc) + '\nPartial removals/drawings may exist. No retry, cleanup or rollback.')
    finally:
        if session is not None and initial is not None:
            try:
                final = session.call('snapshot')
                session._snapshot(final)
                if final['resolution'] != initial['resolution']:
                    # Existing narrowly scoped connector operation, once only.
                    result = subprocess.run([node_executable(), str(connector_directory()/'src/cli/index.js'),
                        'timeframe', initial['resolution']],capture_output=True,text=True,encoding='utf-8',
                        cwd=connector_directory(),timeout=25,stdin=subprocess.DEVNULL,shell=False,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    if result.returncode or json.loads(result.stdout).get('success') is not True:
                        raise ValueError('Timeframe restoration failed; operator assistance required.')
                    restored = session.call('snapshot')
                    session._snapshot(restored)
                    if restored['resolution'] != initial['resolution']:
                        raise ValueError('Timeframe restoration could not be verified; operator assistance required.')
                    final = restored
                old = {d['id']:d for d in initial['drawings'] if d['id'] not in removed}
                now = {d['id']:d for d in final['drawings']}
                if (any(now.get(k) != v for k,v in old.items()) or final['studies'] != initial['studies']
                        or any(g not in final['groups'] for g in initial['groups'] if g['id'] not in removed_groups)):
                    raise ValueError('Final unrelated drawing/group/study preservation failed; operator assistance required.')
            except (OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired) as exc:
                # Propagate through the public adapter so a failed final check
                # overrides any proposed success without returning from finally.
                raise ValueError(sanitize_output(exc)) from None


def _logged_mcp_calls(output):
    calls = []
    for line in output.splitlines():
        try: event = json.loads(line)
        except ValueError: continue
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and item.get('type') == 'mcp_tool_call':
            calls.append(item)
    return calls


def _tool_result(item):
    result = item.get('result', {})
    if isinstance(result.get('structured_content'), dict):
        return result['structured_content']
    for block in result.get('content', []):
        if block.get('type') != 'text': continue
        try: value = json.loads(block['text'])
        except ValueError: continue
        if isinstance(value, dict): return value
    return {}


NO_ELLIOTT_SCENARIOS = 'No Elliott Wave scenarios were found on this chart. Please run Elliott Wave & Fibonacci Analysis first.'


def run_entry_analysis(symbol, model=DEFAULT_MODEL, *, confirmed_chart='', live_confirmed=False, timeout=240):
    """Read-only advisory inquiry using the same executor and independent snapshots."""
    if not live_confirmed:
        return AnalysisOutcome('confirmation_required', errors='Explicit chart confirmation required.')
    try:
        build_support_resistance_prompt(symbol, ['15m'], 1, model, confirmed_chart=confirmed_chart, live_confirmed=True)
        session = GroupingSession(confirmed_chart, verify_tradingview_chart, connector_directory(), node_executable())
        if session.target['symbol'] != symbol: raise ValueError('Selected chart symbol mismatch.')
        with session.exclusive():
            before = session.call('snapshot');session._snapshot(before)
            by_id = {d['id']:d for d in before['drawings']}
            groups = [g for g in before['groups'] if g['name'] in RESERVED_GROUP_NAMES and g['ids']]
            if not groups:
                return AnalysisOutcome('no_scenarios', final_analysis=NO_ELLIOTT_SCENARIOS)
            if len({g['name'] for g in groups}) != len(groups) or any(eid not in by_id for g in groups for eid in g['ids']):
                raise ValueError('Ambiguous/missing scenario membership. Review groups and rerun analysis.')
            relevant = {eid for g in groups for eid in g['ids']}
            context = dict(before, drawings=[{'id':d['id'],'shape':d['shape'],'points':d['points'],
                'text':d['properties'].get('text','')} for d in before['drawings'] if d['id'] in relevant], groups=groups)
            prompt = ('Trade With AM — Check Entry Conditions. READ ONLY. Analysis age is unknown.\n'
                + elliott_direct_tool_guidance(read_only=True) + '\nApproved context: ' + json.dumps(context)
                + '\nInspect current chart state and full OHLCV (count 120, summary:false) on the CURRENT resolution only. '
                'Do not switch timeframe/symbol or draw/modify/delete/group anything. Use actual named-group annotations, '
                'levels and completed candles, excluding the last forming candle for confirmation. Do not infer an entry '
                'merely from group names or a wave count. Identify which existing scenario is developing, if any; confirmation '
                'actually met or outstanding, immediate levels, weakening/invalidation, what to watch and WAIT/MONITOR/conditional '
                'setup only if justified. A conflicting or unreadable/stale scenario requires RE-ANALYZE; missing 15m evidence '
                'cannot confirm a 15m condition from a 1H candle. No Telegram/orders. Tool/data failure is FAILURE, never WAIT. '
                'Return ONLY JSON schema=1, run_id=' + json.dumps(session.run_id) + ', target=' + json.dumps(session.target)
                + ", status='complete', errors=[] only if inspection/data succeed; otherwise incomplete/errors. "
                "entry_status WAIT|MONITOR|CONDITIONAL|RE-ANALYZE; scenario_group exact existing name or null; "
                "levels=array of actual numeric levels referenced from supplied drawing points or retrieved bars; "
                "confirmed=boolean; confirmation_evidence=array of {time,price} actual COMPLETED candle close evidence if "
                "confirmed=true, otherwise []; final_analysis=SHORT copyable trader response. Include all quoted price levels "
                "in levels. Do not claim that conditional consideration is an order or guarantees safety.")
            outcome = _execute_codex(prompt, model, timeout, elliott_wave=True, read_only=True)
            after = session.call('snapshot');session._snapshot(after)
            if not _same_snapshot(before, after): raise ValueError('Read-only chart preservation failed; inspect chart.')
            if outcome.status != 'completed_unverified': return outcome
            calls = _logged_mcp_calls(outcome.output)
            if any(c.get('status') != 'completed' or _tool_result(c).get('success') is False
                   or c.get('server') != 'tradingview' or c.get('tool') in ('draw_shape','chart_set_timeframe')
                   or c.get('tool') not in TRADINGVIEW_SR_TOOLS for c in calls):
                raise ValueError('Unexpected tool route during read-only inquiry.')
            reads = [_tool_result(c).get('bars', []) for c in calls if c.get('tool') == 'data_get_ohlcv'
                     and c.get('arguments', {}).get('summary') is False]
            if not reads or any(len(bars) < 2 for bars in reads): raise ValueError('Insufficient full candle data for entry inquiry.')
            resolution = None
            for call in calls:
                payload = _tool_result(call)
                if call['tool'] == 'chart_get_state':
                    if payload.get('symbol') != symbol or str(payload.get('resolution')) != str(before['resolution']):
                        raise ValueError('Entry candle chart/resolution mismatch.')
                    resolution = str(payload['resolution'])
                if call['tool'] == 'data_get_ohlcv':
                    if resolution is None or call.get('arguments',{}).get('summary') is not False:
                        raise ValueError('Entry data requires current chart verification and full candles.')
                    _validate_bars(payload.get('bars'), 2)
            bars = [bar for read in reads for bar in read]
            prices = [p['price'] for eid in relevant for p in by_id[eid]['points']]
            prices += [bar[k] for bar in bars for k in ('open','high','low','close')]
            data = json.loads(outcome.response)
            if (data.get('schema') != 1 or data.get('run_id') != session.run_id or data.get('target') != session.target
                    or data.get('status') != 'complete' or data.get('errors') != []
                    or data.get('entry_status') not in ('WAIT','MONITOR','CONDITIONAL','RE-ANALYZE')
                    or data.get('scenario_group') not in [None] + [g['name'] for g in groups]
                    or not isinstance(data.get('final_analysis'), str) or len(data['final_analysis'].strip()) < 40
                    or not isinstance(data.get('levels'), list) or any(type(p) not in (int,float)
                        or not any(abs(p-source) < 1e-8 for source in prices) for p in data['levels'])):
                raise ValueError('Entry response/evidence is incomplete or refers to unsupported levels.')
            evidence = data.get('confirmation_evidence')
            completed = [bar for read in reads for bar in read[:-1]]
            if (type(data.get('confirmed')) is not bool or not isinstance(evidence,list)
                    or data['confirmed'] and not evidence or any(not isinstance(e,dict) or not any(
                        e.get('time') == bar['time'] and e.get('price') == bar['close'] for bar in completed) for e in evidence)):
                raise ValueError('Confirmation lacks actual completed-candle close evidence.')
            outcome.status='completed_verified';outcome.analysis_completed=True;outcome.drawings_verified=True
            outcome.final_analysis=data['final_analysis'].strip()
            return outcome
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
        return AnalysisOutcome('failed', errors=sanitize_output(exc) + '\nRead-only inquiry stopped; no automatic retry.')


def run_simple_smc_analysis(symbol, categories, model=DEFAULT_MODEL, *, confirmed_chart='',
                                live_confirmed=False, on_phase=None):
    """Reference CLI/stdin/text-file approach; Astra owns drawings and groups."""
    if not live_confirmed:
        return AnalysisOutcome('confirmation_required', errors='Explicit chart/analysis confirmation required.')
    answers, logs = [], []
    try:
        if (not isinstance(categories,(list,tuple)) or not categories or len(set(categories))!=len(categories)
                or any(c != 'smc' for c in categories)):
            raise ValueError('Select SMC analysis.')
        build_support_resistance_prompt(symbol,['1H'],1,model)  # Reuse input validation only.
        target=target_identity(confirmed_chart)
        if target['symbol']!=symbol:raise ValueError('Selected symbol differs from the approved chart.')
        reference=PROJECT_DIR/'reference_simple'
        files={'smc':'smc_setups.txt'}
        prompts=[(c,(reference/files[c]).read_text(encoding='utf-8-sig').strip())
                 for c in ('smc',) if c in categories]
        if any(not text for _,text in prompts):raise ValueError('A selected reference prompt file is empty.')
        command=codex_command()
        environment=os.environ.copy()
        node=node_executable()
        if node:environment['PATH']=str(Path(node).parent)+os.pathsep+environment.get('PATH','')
        responses=PROJECT_DIR/'responses';responses.mkdir(exist_ok=True)
        folder=Path(tempfile.mkdtemp(prefix='smc_',dir=responses))
        for category,text in prompts:
            if target_identity(verify_tradingview_chart(symbol))!=target:
                raise ValueError('Approved chart identity changed; remaining analysis stopped.')
            # File contents stay editable and unchanged. This short GUI context
            # binds them to the selected chart and the creation-only test scope.
            context=(f'GUI-APPROVED {category.upper()} EXECUTION CONTEXT\n'
                'For this GUI run, these target/safety instructions take precedence over any shorthand group names '
                'or assumptions in the reference prompt above.\n'
                f'Symbol: {symbol}\n'
                f"Tab: {target['tab_id']}\nChart: {target['chart_id']}\nURL: {target['url']}\n"
                f"Layout: {target['layout']}\nPane: {target['pane_index']}\nStandard Candles only.\n"
                'The application has already independently verified the selected chart, layout and pane before launch '
                'and will recheck them after execution. Use chart_get_state and tab_list to confirm the symbol, '
                'standard Candles and approved tab before drawing. Do not call pane_list or repeat the application\'s '
                'layout/pane inspection through another tool; that duplicate inspection is not required for this analysis. '
                'If any MCP action is denied or requires unavailable approval, stop and report it; never work around '
                'a permission denial or retry it. Never switch symbol/tab/layout/pane. '
                'Record and restore the starting timeframe. Create drawings and Object Tree groups directly through the existing MCP. '
                'Creation only: do not delete, replace, modify or regroup existing objects, groups or studies. '
                'If a requested group name is occupied, stop and report the conflict; do not duplicate it. '
                'No Telegram, orders, automatic retry or cleanup. Never invent prices or weak ideas to fill three setups. '
                'Return ordinary human-readable analysis, not a JSON completion contract.\n'
                'Create only qualifying SMC Intraday Setup 1, SMC Intraday Setup 2, SMC Intraday Setup 3 groups. '
                'All six SMC models must be considered regardless of the demonstration checkbox states.\n')
            response_file=folder/(category+'-response.txt')
            if on_phase:on_phase(category)
            completed=subprocess.run(command+['exec','--model',model,'--sandbox','read-only',
                '--skip-git-repo-check','--output-last-message',str(response_file),'-'],
                input=text+'\n\n'+context,text=True,capture_output=True,encoding='utf-8',errors='replace',cwd=reference,
                env=environment,shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            response=response_file.read_text(encoding='utf-8').strip() if response_file.exists() else ''
            logs.append(f'{category.upper()} response file: {response_file}\n'+sanitize_output(completed.stdout))
            if response:answers.append(category.upper()+'\n\n'+response)
            failure=codex_failure(response,completed.stdout,completed.stderr)
            blocked=bool(re.search(r'MCP tool call requires approval|approval review rejected',
                '\n'.join((response,completed.stdout,completed.stderr)),re.I))
            if blocked:
                failure=('MCP approval blocked: an action required approval unavailable in this noninteractive run. '
                         'No permission was bypassed or changed. Review only the rejected tool in Codex before another attempt.')
            if completed.returncode or not response or failure:
                return AnalysisOutcome('approval_blocked' if blocked else 'failed',return_code=completed.returncode,final_analysis='\n\n'.join(answers),
                    output='\n\n'.join(logs),errors=sanitize_output(failure or completed.stderr or
                    'Codex produced no final response.')+'\nRemaining analysis stopped. No automatic retry or cleanup.')
            if target_identity(verify_tradingview_chart(symbol))!=target:
                raise ValueError('Chart identity changed after Codex; remaining analysis stopped.')
        return AnalysisOutcome('completed_response',return_code=0,analysis_completed=True,
            response='\n\n'.join(answers),final_analysis='\n\n'.join(answers),output='\n\n'.join(logs))
    except (OSError,ValueError,TypeError) as exc:
        return AnalysisOutcome('failed',errors=sanitize_output(exc)+'\nNo automatic retry or cleanup.',
            final_analysis='\n\n'.join(answers),output='\n\n'.join(logs))


def run_simple_entry_conditions(symbol, model=DEFAULT_MODEL, *, confirmed_chart='', live_confirmed=False):
    """Same proven CLI arguments/stdin/output mechanism; read-only prompt only."""
    if not live_confirmed:
        return AnalysisOutcome('confirmation_required',errors='Explicit chart confirmation required.')
    response='';logs=''
    try:
        build_support_resistance_prompt(symbol,['1H'],1,model)  # Existing input validation.
        target=target_identity(confirmed_chart)
        if target['symbol']!=symbol:raise ValueError('Selected symbol differs from approved chart.')
        reference=PROJECT_DIR/'reference_simple'
        prompt=(reference/'smc_entry_conditions.txt').read_text(encoding='utf-8-sig').strip()
        if not prompt:raise ValueError('smc_entry_conditions.txt is empty.')
        if target_identity(verify_tradingview_chart(symbol))!=target:
            raise ValueError('Approved chart identity changed; inquiry stopped.')
        environment=os.environ.copy();node=node_executable()
        if node:environment['PATH']=str(Path(node).parent)+os.pathsep+environment.get('PATH','')
        responses=PROJECT_DIR/'responses';responses.mkdir(exist_ok=True)
        folder=Path(tempfile.mkdtemp(prefix='smc_entry_conditions_',dir=responses))
        response_file=folder/'entry-conditions-response.txt'
        context=(f'\n\nAPP-VERIFIED READ-ONLY TARGET\nSymbol: {symbol}\nTab: {target["tab_id"]}\n'
            f'Chart: {target["chart_id"]}\nURL: {target["url"]}\nLayout: {target["layout"]}\nPane: {target["pane_index"]}\n'
            'The application has already verified this exact chart/layout/pane and will recheck it afterwards. '
            'Confirm symbol/standard Candles with chart_get_state and tab with tab_list. Do not duplicate pane_list '
            'or layout/pane inspection through another tool. These read-only restrictions override any instruction '
            'to draw, maintain scenarios or change the chart. Do not bypass a permission denial.')
        command=codex_command()+['exec','--model',model,'--sandbox','read-only','--skip-git-repo-check',
            '--output-last-message',str(response_file),'-']
        completed=subprocess.run(command,input=prompt+context,text=True,capture_output=True,encoding='utf-8',
            errors='replace',cwd=reference,env=environment,shell=False,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        response=response_file.read_text(encoding='utf-8').strip() if response_file.exists() else ''
        logs=f'Entry conditions response file: {response_file}\n'+sanitize_output(completed.stdout)
        failure=codex_failure(response,completed.stdout,completed.stderr)
        blocked=bool(re.search(r'MCP tool call requires approval|approval review rejected',
            '\n'.join((response,completed.stdout,completed.stderr)),re.I))
        if completed.returncode or not response or failure or blocked:
            return AnalysisOutcome('approval_blocked' if blocked else 'failed',return_code=completed.returncode,
                final_analysis=response,output=logs,errors=sanitize_output(failure or completed.stderr or
                    'Entry inquiry failed or requires unavailable approval. No automatic retry.'))
        if target_identity(verify_tradingview_chart(symbol))!=target:
            raise ValueError('Chart identity changed after inquiry; inspect chart before another request.')
        return AnalysisOutcome('completed_response',return_code=0,analysis_completed=True,
            response=response,final_analysis=response,output=logs)
    except (OSError,ValueError,TypeError) as exc:
        return AnalysisOutcome('failed',errors=sanitize_output(exc)+'\nNo automatic retry.',final_analysis=response,output=logs)


def _execute_codex(prompt, model, timeout, *, elliott_wave=False, read_only=False):
    """One CLI invocation; grouping and verification stay in the application."""
    try:
        command = codex_command()
        environment = os.environ.copy()
        node = node_executable()
        if node:
            # The existing MCP config launches "node" by name. Explorer may
            # have a stale PATH, so give this child the discovered Node folder.
            environment["PATH"] = str(Path(node).parent) + os.pathsep + environment.get("PATH", "")
        # A separate CLI process loads the user's existing CODEX_HOME/config.toml.
        # Reuse the connector transport/auth. Override only this invocation's
        # exposed TradingView tools and their approval modes, after consent.
        with tempfile.TemporaryDirectory(prefix="trade_with_am_") as folder:
            final_file = Path(folder) / "response.txt"
            policy = support_resistance_mcp_arguments(read_only=read_only)
            if elliott_wave:
                # Same supported host as the proven STEP 561D/562 runs. Keep
                # the MCP allowlist; do not enable the removed tool_search.
                policy += ["-c", "features.code_mode_host=true", "-c", "features.tool_suggest=false"]
            command += ["exec"] + policy + [
                "--model", model, "--sandbox", "read-only",
                "--skip-git-repo-check", "--color", "never",
                "--output-last-message", str(final_file), "-",
            ]
            if elliott_wave:
                command.insert(-1, "--json")  # Existing executor; inspect actual failure events.
            completed = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", cwd=PROJECT_DIR,
                env=environment,
                timeout=timeout, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # The final message is separate from stdout/tool logs. No log fallback.
            response = sanitize_output(final_file.read_text(encoding="utf-8", errors="replace")) if final_file.exists() else ""
            output = sanitize_output(completed.stdout)
            errors = sanitize_output(completed.stderr)
            status = "completed_unverified" if completed.returncode == 0 else "failed"
            transcript = "\n".join((response, output, errors)).lower()
            if elliott_wave:
                failure = codex_failure(response, output, errors)
                if failure:
                    status = "failed"
                    errors += "\n" + failure + " No automatic retry."
            if "mcp tool call requires approval" in transcript and "approval policy is never" in transcript:
                status = "approval_blocked"
                errors += (
                    "\nCodex reported an MCP tool approval block. No automatic retry was attempted. "
                    "Use Copy Prompt in your interactive Codex/Astra session and approve only the required "
                    "TradingView tools, retaining the exact chart checks. Inspect the chart for partial "
                    "changes before another request. A managed policy may still require interactive approval."
                )
            return AnalysisOutcome(
                status,
                response=response,
                errors=errors,
                return_code=completed.returncode,
                output=output,
                prompt=prompt,
            )
    except subprocess.TimeoutExpired as exc:
        return AnalysisOutcome(
            "timed_out", errors=f"Codex exceeded {timeout} seconds. Partial chart changes may exist. "
            "Inspect TradingView before another request. No automatic retry.\n" + sanitize_output(exc.stderr),
            output=sanitize_output(exc.stdout), prompt=prompt,
        )
    except OSError as exc:
        return AnalysisOutcome("launch_error", errors=sanitize_output(exc), prompt=prompt)
