"""Complete saved-response delivery checks. No model, chart or real Telegram."""
import html
from html.parser import HTMLParser
import importlib.util
import io
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import telegram_sender as tg
from sr_agent import AnalysisOutcome

ROOT=Path(__file__).resolve().parents[1]
REPORT='''# Higher-timeframe context <&>

Daily liquidity remains above **81,460.4**. This paragraph stays complete.

## Turtle Soup — WATCHLIST SETUP

- Entry condition: completed 5m close above 81,460.4.
- Stop loss: 81,100.2; invalidation: acceptance below 81,090.1.
- Targets: 81,900.5 and 82,050.6. Watch the retest next.

| Model | Confirmation | Stop / invalidation | Targets |
|---|---|---|---|
| IFVG | 5m displacement above 81,500.7 | 81,200.3 | 81,950.8 |
| Unicorn | Retest holds above 81,550.9 | 81,250.4 | 82,100.5 |

[Chart](https://example.test/chart?x=1&y=2)

NO TRADE / WAIT — a setup is not a confirmed entry.
'''


class CheckedHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.stack=[];self.text=[]
    def handle_starttag(self,tag,attrs):
        if tag not in ('b','i') or attrs:raise AssertionError('Unexpected Telegram HTML')
        self.stack.append(tag)
    def handle_endtag(self,tag):
        if not self.stack or self.stack.pop()!=tag:raise AssertionError('Unbalanced HTML')
    def handle_data(self,data):self.text.append(data)


def body_text(messages):
    values=[]
    for message in messages:
        check=CheckedHTML();check.feed(message);check.close()
        if check.stack:raise AssertionError('Unclosed Telegram tag')
        body=CheckedHTML();body.feed(message.split('\n\n',1)[1]);body.close()
        values.append(''.join(body.text))
    return ''.join(values)


def report(text=REPORT):
    return dict(analysis_kind='simple_smc',cli_completed=True,categories=['smc'],final_analysis=text)


class FormattingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket,'socket',side_effect=AssertionError('No network')))
        self.enterContext(patch.object(subprocess,'Popen',side_effect=AssertionError('No model or chart subprocess')))

    def test_headings_paragraphs_every_table_cell_and_prices_preserved(self):
        messages=tg.format_smc_response(REPORT,'BYBIT.P','SMC Intraday')
        text=body_text(messages)
        self.assertIn('<b>Higher-timeframe context &lt;&amp;&gt;</b>',messages[0])
        self.assertIn('<b>81,460.4</b>',messages[0])
        for token in ('This paragraph stays complete.\n\n','• Entry condition:',
                      'Model: IFVG','Confirmation: 5m displacement above 81,500.7',
                      'Model: Unicorn','Chart (https://example.test/chart?x=1&y=2)',
                      'NO TRADE / WAIT — a setup is not a confirmed entry.'):
            self.assertIn(token,text)
        for price in re.findall(r'\d[\d,]*\.\d+',REPORT):self.assertIn(price,text)
        self.assertNotIn('|---',text)
        self.assertEqual(tg.format_trade_messages(report(),'BYBIT.P',[],chart_verified=False),messages)

    def test_existing_saved_smc_report_is_retained_in_full(self):
        path=os.environ.get('SMC_TEST_RESPONSE')
        candidates=[Path(path)] if path else sorted(ROOT.glob('responses/smc_*/smc-response.txt'))
        if not candidates:self.skipTest('No local saved response available; synthetic report still checked')
        source=candidates[0].read_text(encoding='utf-8-sig')
        result=body_text(tg.format_smc_response(source,'BYBIT.P'))
        # Exact complete formatted-body equality catches any omitted/reordered tail.
        expected=CheckedHTML();expected.feed(tg._format_complete_markdown(source))
        self.assertEqual(result,''.join(expected.text))
        for price in re.findall(r'\d[\d,]*\.\d+',source):self.assertIn(price,result)
        self.assertGreater(len(result),len(source)*.8)

    def test_long_unicode_report_reassembles_and_every_part_is_valid(self):
        source=REPORT+('\n\n**🟢 <&> IFVG** Confirmation 81,460.4. '*1800)+'\n\nCOMPLETE FINAL LINE.'
        messages=tg.format_smc_response(source,'BYBIT:BTCUSDT.P')
        self.assertGreater(len(messages),20)
        expected=CheckedHTML();expected.feed(tg._format_complete_markdown(source))
        self.assertEqual(body_text(messages),''.join(expected.text))
        for index,message in enumerate(messages,1):
            self.assertLessEqual(tg._units(message),4096)
            self.assertIn(f'({index}/{len(messages)})',message)

    def test_delivery_is_sequential_and_stops_at_first_failure(self):
        value=report(REPORT*10);expected=tg.format_trade_messages(value,'BYBIT.P',[],chart_verified=False)
        with patch.object(tg,'load_config',return_value=tg.TelegramConfig('123456:'+'X'*30,'12345')),\
             patch.object(tg,'_request',side_effect=range(1,len(expected)+1)) as request:
            result=tg.send_trade_analysis(value,'BYBIT.P',[],chart_verified=False,live_send_enabled=True)
        self.assertTrue(result.accepted);self.assertEqual([c.args[1] for c in request.call_args_list],expected)
        with patch.object(tg,'load_config',return_value=tg.TelegramConfig('123456:'+'X'*30,'12345')),\
             patch.object(tg,'_request',side_effect=[1,tg.TelegramError('Part 2 unconfirmed')]) as request:
            result=tg.send_trade_analysis(value,'BYBIT.P',[],chart_verified=False,live_send_enabled=True)
        self.assertFalse(result.accepted);self.assertEqual(result.accepted_parts,1)
        self.assertEqual(result.required_parts,len(expected));self.assertEqual(request.call_count,2)

    def test_failure_and_incomplete_notification_gate(self):
        for status,code,completed,text in (('failed',1,False,REPORT),('completed_response',0,False,REPORT),
              ('completed_response',0,True,'Analysis was interrupted.'),('completed_response',0,True,'')):
            self.assertFalse(tg.simple_analysis_complete(AnalysisOutcome(status,return_code=code,analysis_completed=completed,final_analysis=text)))
        self.assertIn('telegram_config.local.json',(ROOT/'.gitignore').read_text())


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root/'tests').mkdir();(self.root/'responses').mkdir()
        (self.root/'responses/smc-response.txt').write_text(REPORT,encoding='utf-8')
        spec=importlib.util.spec_from_file_location('smc_manual',ROOT/'tests/manual_telegram_test.py')
        self.manual=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.manual)
        self.manual.SEND_TELEGRAM=False
        self.manual.PROJECT_ROOT=self.root;self.manual.RESPONSE_FILE='responses/smc-response.txt'
        self.enterContext(patch.object(socket,'socket',side_effect=AssertionError('No network')))
        self.enterContext(patch.object(subprocess,'Popen',side_effect=AssertionError('No Codex/chart')))

    def test_preview_relative_path_shared_formatter_no_credentials_or_sender(self):
        with patch.object(self.manual,'format_smc_response',wraps=tg.format_smc_response) as formatter,\
             patch.object(self.manual,'send_trade_analysis') as send,patch.object(tg,'load_config') as load,\
             patch('sys.stdout',new=io.StringIO()) as output:
            self.assertEqual(self.manual.main(),0)
        formatter.assert_called_once_with(REPORT,'BYBIT.P','SMC Intraday')
        send.assert_not_called();load.assert_not_called()
        expected='\n\n----- NEXT TELEGRAM MESSAGE -----\n\n'.join(tg.format_trade_messages(report(),'BYBIT.P',[],chart_verified=False))
        self.assertEqual((self.root/'tests/telegram_preview.txt').read_text(encoding='utf-8'),expected)
        self.assertIn('SEND_TELEGRAM=False',output.getvalue())

    def test_opt_in_absolute_path_same_sender_and_partial_failure_reporting(self):
        self.manual.RESPONSE_FILE=str(self.root/'responses/smc-response.txt');self.manual.SEND_TELEGRAM=True
        with patch.object(tg,'load_config',return_value=tg.TelegramConfig('123456:'+'X'*30,'12345')) as load,\
             patch.object(tg,'_request',return_value=123) as request,patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(self.manual.main(),0)
        load.assert_called_once_with(None)
        self.assertEqual(request.call_args.args[1],(self.root/'tests/telegram_preview.txt').read_text(encoding='utf-8'))
        (self.root/'responses/smc-response.txt').write_text(REPORT*10,encoding='utf-8')
        with patch.object(tg,'load_config',return_value=tg.TelegramConfig('123456:'+'X'*30,'12345')),\
             patch.object(tg,'_request',side_effect=[1,tg.TelegramError('Part 2 unconfirmed')]) as request,\
             patch('sys.stdout',new=io.StringIO()) as output:
            self.assertEqual(self.manual.main(),1)
        self.assertEqual(request.call_count,2);self.assertIn('NOT complete (1/',output.getvalue())


if __name__=='__main__':unittest.main()
