import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml

REPO=Path(__file__).resolve().parents[1]


class CliProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.env=dict(os.environ, PYTHONPATH=str(REPO))

    def document(self, code='pass', transitions=None):
        return {'version':1,'initial':'verify','executors':{'cmd':{'type':'command','executable':sys.executable,'argv':['-c',code],'timeout_seconds':5}},
                'states':{'verify':{'executor':'cmd','on':transitions if transitions is not None else {'success':'escalated'}},
                          'accepted':{'terminal':True},'escalated':{'terminal':True}}}

    def cli(self,*args,cwd=None):
        return subprocess.run([sys.executable,'-m','switchyard',*args],cwd=cwd or self.root,env=self.env,capture_output=True,text=True,timeout=10)

    def test_real_exit_codes_and_streams(self):
        file=self.root/'flow.yaml'
        file.write_text(yaml.safe_dump(self.document("from pathlib import Path; Path('marker').touch()")))
        result=self.cli('validate','flow.yaml')
        self.assertEqual((result.returncode,result.stderr),(0,'')); self.assertEqual(result.stdout,f'Valid: {file}\n')
        self.assertFalse((self.root/'marker').exists())
        for terminal in ('accpeted','verify'):
            result=self.cli('run','flow.yaml','--task','secret','--require-terminal',terminal)
            self.assertEqual((result.returncode,result.stdout),(2,''))
            self.assertFalse((self.root/'marker').exists())
        for extras,status in (([],0),(['--require-terminal','accepted'],1),(['--require-terminal','accepted','--require-terminal','escalated'],0)):
            result=self.cli('run','flow.yaml','--task','secret',*extras)
            self.assertEqual((result.returncode,result.stderr),(status,''))
            self.assertRegex(result.stdout,r'^Run: run:[0-9a-f-]+\nTerminal: escalated\n')
            if status==1: self.assertTrue(result.stdout.endswith('Required terminal: accepted\n'))
        file.write_text('version: 2')
        result=self.cli('validate','flow.yaml')
        self.assertEqual((result.returncode,result.stdout),(2,'')); self.assertIn('unsupported flow configuration version: 2',result.stderr)
        self.assertNotIn('Traceback',result.stderr)
        file.write_text(yaml.safe_dump(self.document(transitions={})))
        result=self.cli('run','flow.yaml','--task','SECRET TASK')
        self.assertEqual((result.returncode,result.stdout,result.stderr),(3,'','switchyard: runtime execution failed\n'))

    def test_usage_help_and_unsafe_terminal(self):
        result=self.cli('run','missing')
        self.assertEqual(result.returncode,2); self.assertEqual(result.stdout,'')
        result=self.cli('--help')
        self.assertEqual(result.returncode,0); self.assertEqual(result.stderr,'')
        name=' strange,\n\t"name\\ '
        file=self.root/'flow.yaml'
        file.write_text(yaml.safe_dump({'version':1,'initial':name,'executors':{},'states':{name:{'terminal':True}}}))
        result=self.cli('run','flow.yaml','--task','','--require-terminal',name)
        self.assertEqual(result.returncode,0)
        self.assertEqual(result.stdout.splitlines()[1], 'Terminal: '+json.dumps(name,ensure_ascii=True))
        self.assertEqual(len(result.stdout.splitlines()),2)

    def test_symlink_addressed_workspace_without_chdir(self):
        a,b,c=[self.root/name for name in ('A','B','caller')]
        for directory in (a,b,c): directory.mkdir()
        file=a/'flow.yaml'; link=b/'flow.yaml'
        file.write_text(yaml.safe_dump(self.document("from pathlib import Path; Path('marker').touch()")))
        try: link.symlink_to(file)
        except OSError: self.skipTest('symlink creation unavailable')
        result=self.cli('run','../B/flow.yaml','--task','t',cwd=c)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((b/'marker').exists()); self.assertFalse((a/'marker').exists()); self.assertFalse((c/'marker').exists())
        result=self.cli('validate','../B/flow.yaml',cwd=c)
        self.assertEqual(result.stdout,f'Valid: {link}\n')

    def test_events_append_order_and_relative_caller_path(self):
        directory = self.root / 'flows'
        directory.mkdir()
        file = directory / 'flow.yaml'
        data = self.document("print('PRIVATE DIAGNOSTIC')")
        data['executors']['cmd']['diagnostic_capture_max_bytes'] = 4096
        file.write_text(yaml.safe_dump(data))
        result = self.cli('run', str(file), '--task', 'PRIVATE TASK')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.root.iterdir()), {directory})
        path = self.root / 'events.jsonl'
        kinds = ['run_started', 'state_entered', 'invocation_started',
                 'invocation_finished', 'transition_selected', 'state_exited',
                 'state_entered', 'state_exited', 'run_finished']
        prefix = b''
        ids = []
        for extras, status in (([], 0), (['--require-terminal', 'accepted'], 1)):
            result = self.cli('run', str(file), '--task', 'PRIVATE TASK',
                              '--events-file', 'events.jsonl', *extras)
            self.assertEqual((result.returncode, result.stderr), (status, ''))
            self.assertNotIn('PRIVATE', result.stdout)
            self.assertTrue(path.read_bytes().startswith(prefix))
            records = [json.loads(line) for line in path.read_bytes()[len(prefix):].splitlines()]
            self.assertEqual([event['kind'] for event in records], kinds)
            run_id = result.stdout.splitlines()[0].removeprefix('Run: ')
            self.assertEqual({event['run_id'] for event in records}, {run_id})
            ids.append(run_id)
            prefix = path.read_bytes()
        self.assertEqual(len(set(ids)), 2)
        self.assertFalse((directory / 'events.jsonl').exists())
        self.assertNotIn('PRIVATE', path.read_text())

    def test_validation_never_touches_event_file(self):
        file = self.root / 'flow.yaml'
        file.write_text(yaml.safe_dump(self.document("from pathlib import Path; Path('marker').touch()")))
        path = self.root / 'events.jsonl'
        for existing in (False, True):
            if existing:
                path.write_bytes(b'existing history\n')
            for args, status in ((['validate', 'flow.yaml'], 0),
                                 (['validate', 'flow.yaml', '--events-file', str(path)], 2),
                                 (['run', 'flow.yaml', '--task', 't', '--events-file', str(path), '--require-terminal', 'typo'], 2),
                                 (['run', 'missing.yaml', '--task', 't', '--events-file', str(path)], 2),
                                 (['run', '--help', '--events-file', str(path)], 0)):
                result = self.cli(*args)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertFalse((self.root / 'marker').exists())
                self.assertEqual(path.read_bytes() if path.exists() else None,
                                 b'existing history\n' if existing else None)

    def test_recording_failure_and_runtime_failure_output(self):
        file = self.root / 'flow.yaml'
        file.write_text(yaml.safe_dump(self.document("from pathlib import Path; Path('marker').touch()")))
        for destination in (self.root, self.root / 'missing' / 'events.jsonl'):
            result = self.cli('run', 'flow.yaml', '--task', 'PRIVATE TASK', '--events-file', str(destination))
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (3, '', 'switchyard: runtime execution failed\n'))
            self.assertFalse((self.root / 'marker').exists())
        self.assertFalse((self.root / 'missing').exists())
        data = self.document("print('PRIVATE DIAGNOSTIC')", transitions={})
        data['executors']['cmd']['diagnostic_capture_max_bytes'] = 4096
        file.write_text(yaml.safe_dump(data))
        path = self.root / 'events.jsonl'
        result = self.cli('run', 'flow.yaml', '--task', 'PRIVATE TASK', '--events-file', str(path))
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (3, '', 'switchyard: runtime execution failed\n'))
        self.assertEqual([json.loads(line)['kind'] for line in path.read_text().splitlines()],
                         ['run_started', 'state_entered', 'invocation_started', 'run_error'])
        self.assertNotIn('PRIVATE', path.read_text())
