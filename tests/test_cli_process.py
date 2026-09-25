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
