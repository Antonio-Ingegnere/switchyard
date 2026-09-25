import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml
from flow_config import FlowConfigError, LoadedFlow
from runtime import Event, ExecutionError, Flow, RunResult, State
from switchyard.cli import main
from manual_smoke.codex_repair_smoke import repair_flow, TASK, VERIFIER_CODE, diagnostic_argv


class CliTests(unittest.TestCase):
    def setUp(self):
        self.loaded = LoadedFlow(Flow('accepted', {
            'accepted': State(terminal=True), 'escalated': State(terminal=True),
            'verify': State('x', {'success':'accepted'})}), {})

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            status = main(args)
        return status,out.getvalue(),err.getvalue()

    def result(self, terminal='accepted', events=None):
        return RunResult(terminal, tuple(events if events is not None else [Event('run_started','run:test')]))

    def test_validation_order_task_and_no_recorder(self):
        with patch('switchyard.cli.load_flow',return_value=self.loaded) as load, patch('switchyard.cli.WorkUnit') as work, patch('switchyard.cli.run',return_value=self.result()) as execute:
            for args in (['validate','file.yaml'], ['run','file.yaml','--task','t','--require-terminal','accepted','--require-terminal','typo'], ['run','file.yaml','--task','t','--require-terminal','verify']):
                self.invoke(args)
                work.assert_not_called(); execute.assert_not_called()
            self.assertEqual(load.call_count,3)
        with patch('switchyard.cli.load_flow',return_value=self.loaded), patch('switchyard.cli.run',return_value=self.result()) as execute:
            for task in ('', 'literal ; $HOME\nTASK'):
                status,_,_=self.invoke(['run','f','--task',task])
                self.assertEqual(status,0)
                self.assertEqual(execute.call_args.args[0].task,task)
                self.assertIs(execute.call_args.args[1],self.loaded.flow)
                self.assertIs(execute.call_args.args[2],self.loaded.executors)
                self.assertEqual(execute.call_args.kwargs,{})

    def test_terminal_membership_and_safe_names(self):
        names=['line\nnext','tab\tname','a,b',' padded ', 'a"b', 'a\\b']
        for name in names:
            loaded=LoadedFlow(Flow(name,{name:State(terminal=True),'other':State(terminal=True)}),{})
            with patch('switchyard.cli.load_flow',return_value=loaded), patch('switchyard.cli.run',return_value=self.result(name)):
                status,out,err=self.invoke(['run','f','--task','secret','--require-terminal',name])
                self.assertEqual((status,err),(0,''))
                self.assertEqual(out.splitlines(),['Run: run:test','Terminal: '+json.dumps(name,ensure_ascii=True)])
                status,out,_=self.invoke(['run','f','--task','secret','--require-terminal','other'])
                self.assertEqual(status,1)
                self.assertEqual(len(out.splitlines()),3)
            with patch('switchyard.cli.load_flow',return_value=loaded), patch('switchyard.cli.run',return_value=self.result('other')):
                _,out,_=self.invoke(['run','f','--task','t','--require-terminal',name])
                self.assertEqual(out.splitlines()[-1],'Required terminal: '+json.dumps(name,ensure_ascii=True))
        with patch('switchyard.cli.load_flow',return_value=self.loaded), patch('switchyard.cli.run',return_value=self.result('escalated')):
            for required,expected in (([],0),(['accepted'],1),(['accepted','escalated','accepted'],0)):
                args=['run','f','--task','t']
                for value in required: args += ['--require-terminal',value]
                self.assertEqual(self.invoke(args)[0],expected)

    def test_semantic_run_id_and_invariant_failure(self):
        for events in ([Event('state_entered','wrong'),Event('run_started','run:real')], [], [Event('run_started','a'),Event('run_started','b')]):
            with patch('switchyard.cli.load_flow',return_value=self.loaded), patch('switchyard.cli.run',return_value=self.result(events=events)):
                if len(events)==2 and events[0].kind != 'run_started':
                    self.assertIn('Run: run:real',self.invoke(['run','f','--task','t'])[1])
                else:
                    with self.assertRaisesRegex(RuntimeError,'exactly one'): self.invoke(['run','f','--task','t'])

    def test_known_errors_and_unexpected_propagation(self):
        with patch('switchyard.cli.load_flow',side_effect=FlowConfigError('bad\nconfig\t'+'x'*1000)):
            status,out,err=self.invoke(['validate','f'])
            self.assertEqual((status,out),(2,'')); self.assertEqual(len(err.splitlines()),1)
            self.assertLessEqual(len(err.rstrip('\n')),512)
        error=ExecutionError('TASK SECRET DIAGNOSTIC', (Event('run_started','secret'),))
        error.__cause__=ValueError('SECRET')
        with patch('switchyard.cli.load_flow',return_value=self.loaded), patch('switchyard.cli.run',side_effect=error):
            self.assertEqual(self.invoke(['run','f','--task','TASK SECRET']), (3,'','switchyard: runtime execution failed\n'))
        bug=RuntimeError('bug')
        with patch('switchyard.cli.load_flow',side_effect=bug):
            with self.assertRaises(RuntimeError) as caught: self.invoke(['validate','f'])
            self.assertIs(caught.exception,bug)

    def test_output_write_and_flush_failures_propagate(self):
        for method in ('write','flush'):
            for args in (['validate','f'],['run','f','--task','t'],['run','f','--task','t','--require-terminal','accepted']):
                stream=Mock(); getattr(stream,method).side_effect=BrokenPipeError('output failed')
                with patch('switchyard.cli.load_flow',return_value=self.loaded), patch('switchyard.cli.run',return_value=self.result('escalated')), patch('sys.stdout',stream):
                    with self.assertRaises(BrokenPipeError): main(args)
            stream=Mock(); getattr(stream,method).side_effect=OSError('stderr failed')
            with patch('switchyard.cli.load_flow',side_effect=FlowConfigError('bad')),patch('sys.stderr',stream):
                with self.assertRaises(OSError): main(['validate','f'])

    def test_argparse_help_usage_and_abbreviation(self):
        for args,expected in ((['--help'],0),([],2),(['run','f'],2),(['run','f','--ta','t'],2),(['validate','f','--task','t'],2)):
            with patch('switchyard.cli.load_flow') as load:
                with self.assertRaises(SystemExit) as caught: self.invoke(args)
                self.assertEqual(caught.exception.code,expected)
                load.assert_not_called()

    def test_bounded_repair_cli_offline(self):
        for repaired,required,status in ((True,[],0),(False,[],0),(False,['accepted'],1),(True,['accepted'],0),(False,['accepted','escalated'],0)):
            with TemporaryDirectory() as tmp:
                workspace=Path(tmp).resolve(); executable=str(workspace/'fake-codex')
                flow=repair_flow()
                data={'version':1,'initial':'verify','executors':{
                    'verify':{'type':'command','executable':sys.executable,'argv':['-c',VERIFIER_CODE], 'timeout_seconds':5,'diagnostic_capture_max_bytes':4096},
                    'codex':{'type':'codex','executable':executable,'timeout_seconds':5}},
                    'states':{name:({'terminal':True} if state.terminal else {'executor':state.executor,'on':dict(state.on),'max_state_visits_per_run':state.max_state_visits_per_run,'diagnostics_from_previous_max_bytes':state.diagnostics_from_previous_max_bytes}) for name,state in flow.states.items()}}
                file=workspace/'flow.yaml'; file.write_text(yaml.safe_dump(data))
                (workspace/'result.txt').write_bytes(b'broken\n')
                real_popen=subprocess.Popen; calls=[]
                def launch(args,**kwargs):
                    if args[0]==executable:
                        self.assertEqual(args,diagnostic_argv(executable)); calls.append(args)
                        code="from pathlib import Path; Path('result.txt').write_bytes(b'runtime-009-ok\\n')" if repaired else 'print("accepted")'
                        args=[sys.executable,'-c',code]
                    else: self.assertEqual(args[0],sys.executable)
                    return real_popen(args,**kwargs)
                args=['run',str(file),'--task',TASK]
                for terminal in required: args += ['--require-terminal',terminal]
                with patch('runtime.command.subprocess.Popen',side_effect=launch): actual,out,err=self.invoke(args)
                self.assertEqual((actual,err),(status,'')); self.assertEqual(len(calls),1)
                self.assertIn('Terminal: '+('accepted' if repaired else 'escalated'),out)
