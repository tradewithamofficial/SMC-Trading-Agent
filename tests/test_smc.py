"""Focused SMC V1 checks: real GUI widgets, all external operations mocked."""
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import ANY,patch

import main
import sr_agent as sr
import sr_grouping as grouping
import telegram_sender as tg

ROOT=Path(__file__).resolve().parents[1]
MODELS=('Turtle Soup','IFVG','ICT 2022','CRT','Silver Bullet','Unicorn')
TARGET=dict(symbol=sr.DEFAULT_SYMBOL,tab_id='TEST-TAB',chart_id='TEST',url='https://www.tradingview.com/chart/TEST/',
            layout='s',pane_index=0,chart_type=1)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        reference=self.folder/'reference_simple';reference.mkdir()
        for name in ('smc_setups.txt','smc_entry_conditions.txt'):
            (reference/name).write_bytes((ROOT/'reference_simple'/name).read_bytes())
        self.enterContext(patch.object(socket,'socket',side_effect=AssertionError('No real network')))
        self.enterContext(patch.object(sr,'PROJECT_DIR',self.folder))
        self.verify=self.enterContext(patch.object(sr,'verify_tradingview_chart',return_value=json.dumps(TARGET)))
        self.enterContext(patch.object(sr,'codex_command',return_value=['codex.exe']))
        self.enterContext(patch.object(sr,'node_executable',return_value=None))
        self.enterContext(patch.object(sr,'_execute_codex',side_effect=AssertionError('No legacy executor')))
        self.enterContext(patch.object(sr,'TradeSetupSession',side_effect=AssertionError('No drawing parser/grouping session')))
        def complete(command,**kwargs):
            Path(command[-2]).write_text('WATCHLIST SETUP — NO TRADE / WAIT. Require completed 5m close above 101.25; invalidation 99.75.',encoding='utf-8')
            return subprocess.CompletedProcess(command,0,'Mock CLI completed','')
        self.run=self.enterContext(patch.object(sr.subprocess,'run',side_effect=complete))

    def analysis(self):
        return sr.run_simple_smc_analysis(TARGET['symbol'],['smc'],'gpt-6-sol',confirmed_chart=json.dumps(TARGET),live_confirmed=True)

    def test_one_proven_cli_call_selected_model_full_prompt_and_smc_response_path(self):
        value=self.analysis();self.assertEqual(value.status,'completed_response',value.errors)
        self.run.assert_called_once();call=self.run.call_args;command=call.args[0]
        self.assertEqual(command[:7],['codex.exe','exec','--model','gpt-6-sol','--sandbox','read-only','--skip-git-repo-check'])
        self.assertEqual(command[-3],'--output-last-message');self.assertEqual(command[-1],'-')
        path=Path(command[-2]);self.assertEqual(path.name,'smc-response.txt');self.assertTrue(path.parent.name.startswith('smc_'))
        self.assertTrue(path.is_file());self.assertEqual(call.kwargs['cwd'],self.folder/'reference_simple')
        self.assertFalse(call.kwargs['shell']);self.assertEqual(call.kwargs['encoding'],'utf-8')
        prompt=call.kwargs['input']
        for token in (*MODELS,'1D','4H','1H','15m','5m','summary:false','WATCHLIST SETUP','CONFIRMED TRIGGER','SMC Intraday Setup 3'):
            self.assertIn(token,prompt)
        self.assertIn(TARGET['tab_id'],prompt);self.assertIn('Do not call pane_list',prompt)
        self.assertNotIn('Elliott Wave Intraday Scenario',prompt);self.assertEqual(self.verify.call_count,2)
        self.assertFalse(value.drawings_verified);self.assertTrue(tg.simple_analysis_complete(value))

    def test_editable_prompt_reloaded_and_wrong_chart_or_missing_file_stops(self):
        path=self.folder/'reference_simple/smc_setups.txt';path.write_text('LATEST EDIT: all six models',encoding='utf-8')
        self.analysis();self.assertIn('LATEST EDIT',self.run.call_args.kwargs['input'])
        self.run.reset_mock();self.verify.return_value=json.dumps(dict(TARGET,tab_id='WRONG'))
        self.assertEqual(self.analysis().status,'failed');self.run.assert_not_called()
        self.verify.return_value=json.dumps(TARGET);path.unlink()
        self.assertEqual(self.analysis().status,'failed');self.run.assert_not_called()

    def test_approval_or_execution_errors_are_failure_never_successful_wait(self):
        for code,text,expected in ((1,'Execution failed','failed'),(0,'unsupported call: tool_searchtool_search_tool','failed'),
                                   (0,'Automatic approval review rejected the MCP action','approval_blocked')):
            self.run.reset_mock()
            def fail(command,**kwargs):
                Path(command[-2]).write_text(text,encoding='utf-8')
                return subprocess.CompletedProcess(command,code,'','')
            self.run.side_effect=fail;value=self.analysis()
            self.assertEqual(value.status,expected);self.assertFalse(value.analysis_completed)
            self.assertFalse(tg.simple_analysis_complete(value));self.run.assert_called_once()

    def test_entry_prompt_readonly_separate_file_and_same_cli_model(self):
        value=sr.run_simple_entry_conditions(TARGET['symbol'],'gpt-6-sol',confirmed_chart=json.dumps(TARGET),live_confirmed=True)
        self.assertEqual(value.status,'completed_response',value.errors);self.run.assert_called_once()
        call=self.run.call_args;path=Path(call.args[0][-2])
        self.assertEqual(path.name,'entry-conditions-response.txt');self.assertTrue(path.parent.name.startswith('smc_entry_conditions_'))
        self.assertEqual(call.args[0][3],'gpt-6-sol')
        text=call.kwargs['input']
        for token in ('strictly READ-ONLY','No SMC setups were found','SMC Intraday Setup 1','SMC Intraday Setup 3',
                      'Do not switch timeframes','No order placement','Telegram','WATCHLIST SETUP','CONFIRMED TRIGGER'):
            self.assertIn(token,text)


class TelegramTests(unittest.TestCase):
    def setUp(self):self.enterContext(patch.object(socket,'socket',side_effect=AssertionError('No network')))

    def test_reused_full_response_and_sequential_sender(self):
        text=('**SMC setup**\n\nWATCHLIST SETUP — NO TRADE / WAIT. 101.25 trigger, 99.75 invalidation.\n\n'*100)
        messages=tg.format_smc_response(text,TARGET['symbol'])
        self.assertGreater(len(messages),1)
        for i,message in enumerate(messages,1):
            self.assertIn('Trade With AM — SMC Trading Agent',message)
            self.assertIn(f'({i}/{len(messages)})',message);self.assertLessEqual(tg._units(message),4096)
        report=dict(analysis_kind='simple_smc',cli_completed=True,categories=['smc'],final_analysis=text)
        with patch.object(tg,'load_config',return_value=tg.TelegramConfig('123456:'+'X'*30,'12345')),patch.object(tg,'_request',side_effect=range(1,100)) as request:
            result=tg.send_trade_analysis(report,TARGET['symbol'],['1H'],chart_verified=False,live_send_enabled=True)
        self.assertTrue(result.accepted);self.assertEqual([c.args[1] for c in request.call_args_list],messages)

    def test_missing_smc_private_config_refuses_network_and_returns_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(tg,'local_config_path',return_value=Path(folder)/'telegram_config.local.json'),patch.object(tg,'_request') as request:
            result=tg.send_trade_analysis(dict(analysis_kind='simple_smc',cli_completed=True,categories=['smc'],final_analysis='NO TRADE / WAIT'),TARGET['symbol'],[],chart_verified=False,live_send_enabled=True)
        self.assertFalse(result.accepted);self.assertIn('Missing telegram_config.local.json',result.error);request.assert_not_called()


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket,'socket',side_effect=AssertionError('No network')))
        self.enterContext(patch.object(main,'get_tradingview_watchlist',return_value=dict(symbols=[sr.DEFAULT_SYMBOL],source='fallback',name='',error='Offline fixture')))
        self.enterContext(patch.object(main,'load_codex_models',return_value=dict(models=[sr.DEFAULT_MODEL,'gpt-6-sol'],source='offline',fetched_at='offline',error='')))
        self.enterContext(patch.object(main,'inspect_tradingview_chart',return_value=json.dumps(TARGET)))
        self.chart=self.enterContext(patch.object(main,'ensure_selected_symbol',return_value=json.dumps(TARGET)))
        self.left=self.enterContext(patch.object(main,'run_simple_smc_analysis',return_value=sr.AnalysisOutcome('completed_response',return_code=0,analysis_completed=True,final_analysis='WATCHLIST SETUP — NO TRADE / WAIT')))
        self.right=self.enterContext(patch.object(main,'run_simple_entry_conditions',return_value=sr.AnalysisOutcome('completed_response',return_code=0,analysis_completed=True,final_analysis='MONITOR — trigger not confirmed.')))
        self.send=self.enterContext(patch.object(main,'send_trade_analysis',return_value=tg.DeliveryOutcome('accepted',1,1,(123,))))
        self.confirm=self.enterContext(patch.object(main.messagebox,'askokcancel',return_value=True))
        self.warning=self.enterContext(patch.object(main.messagebox,'showwarning'))
        self.error=self.enterContext(patch.object(main.messagebox,'showerror'))
        self.enterContext(patch.object(main.messagebox,'showinfo'))
        self.root=main.ctk.CTk();self.root.withdraw();self.app=main.SupportResistanceApp(self.root)
        self.addCleanup(self.close);self.pump(lambda:not self.app.refreshing and bool(self.app.model.get()))

    def close(self):
        for callback in self.root.tk.call('after','info'):self.root.tk.call('after','cancel',callback)
        self.root.destroy()

    def pump(self,predicate,timeout=4):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.root.update()
            if predicate():return
            time.sleep(.005)
        self.fail('Offline GUI worker did not complete.')

    def test_startup_labels_six_defaults_artwork_and_build_paths(self):
        self.assertEqual(self.root.title(),'Trade With AM — SMC Trading Agent')
        self.assertEqual(tuple(self.app.smc_models),MODELS)
        self.assertTrue(all(v.get() for v in self.app.smc_models.values()))
        self.assertEqual(tuple(box.cget('text') for box in self.app.smc_model_boxes.values()),MODELS)
        self.assertEqual(self.app.find_button.cget('text'),'Find SMC Trade Setups')
        self.assertEqual(self.app.trade_button.cget('text'),'Check Entry Conditions')
        self.assertIsNotNone(self.app.agent_image)
        self.assertTrue((ROOT/'assets/TradeWithAM_Robot.ico').is_file())
        self.root.deiconify();self.root.update_idletasks();self.assertTrue(self.root.winfo_ismapped());self.root.withdraw()
        build=(ROOT/'build.ps1').read_text(encoding='utf-8')
        for token in ('--name SMCTradingAgent','smc_setups.txt','smc_entry_conditions.txt'):self.assertIn(token,build)
        self.left.assert_not_called();self.right.assert_not_called();self.send.assert_not_called()

    def test_all_checkboxes_deselected_still_runs_one_smc_prompt_and_left_only_sender(self):
        for box in self.app.smc_model_boxes.values():box.deselect()
        self.app.model.set('gpt-6-sol')
        with patch.object(self.app,'show_trade_results') as popup:
            self.app.find_button.invoke();self.pump(lambda:not self.app.running)
        self.left.assert_called_once_with(TARGET['symbol'],['smc'],'gpt-6-sol',confirmed_chart=json.dumps(TARGET),live_confirmed=True,on_phase=ANY)
        self.send.assert_called_once();self.assertEqual(self.send.call_args.args[0]['analysis_kind'],'simple_smc')
        self.right.assert_not_called();self.error.assert_not_called();popup.assert_called_once()
        self.assertIn('demonstration-only',self.confirm.call_args.args[1])

    def test_right_ignores_demo_boxes_copy_popup_and_no_telegram(self):
        self.app.model.set('gpt-6-sol')
        with patch.object(self.app,'show_trade_results') as popup:
            for selected in (False,True):
                for variable in self.app.smc_models.values():variable.set(selected)
                self.app.trade_button.invoke();self.pump(lambda:not self.app.running)
            self.assertEqual(popup.call_count,2)
        self.assertEqual(self.right.call_count,2);self.left.assert_not_called();self.send.assert_not_called()
        self.right.assert_called_with(TARGET['symbol'],'gpt-6-sol',confirmed_chart=json.dumps(TARGET),live_confirmed=True)

    def test_left_failure_and_cancel_do_not_notify_and_all_charts_stays_blocked(self):
        self.left.return_value=sr.AnalysisOutcome('failed',errors='Offline tool failure')
        self.app.find_levels();self.pump(lambda:not self.app.running);self.warning.assert_called_once();self.send.assert_not_called()
        self.left.reset_mock();self.chart.reset_mock();self.confirm.return_value=False
        self.app.find_levels();self.pump(lambda:not self.app.running)
        self.left.assert_not_called();self.chart.assert_not_called()
        self.app.all_charts.set(True);self.app.find_levels();self.left.assert_not_called()

    def test_background_worker_keeps_gui_responsive_and_blocks_overlap(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def slow(*args,**kwargs):
            entered.set();release.wait(3)
            return sr.AnalysisOutcome('failed',errors='Offline stopped')
        self.left.side_effect=slow
        self.app.find_levels();self.pump(entered.is_set)
        ticks=[];self.root.after(1,lambda:ticks.append(True));self.pump(lambda:bool(ticks))
        self.assertTrue(self.app.running);self.app.find_levels();self.left.assert_called_once()
        release.set();self.pump(lambda:not self.app.running);self.send.assert_not_called()


class IdentityTests(unittest.TestCase):
    def test_runtime_namespace_and_private_config_location(self):
        self.assertIn('SMCTradingAgentV1',str(grouping.record_directory()))
        self.assertEqual(tg.local_config_path(),ROOT/'telegram_config.local.json')
        self.assertEqual(json.loads((ROOT/'telegram_config.example.json').read_text()),dict(bot_token='',chat_id=''))


if __name__=='__main__':unittest.main()
