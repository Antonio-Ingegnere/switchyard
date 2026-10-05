"""V2-006 offline: actual fake-provider processes and actual unittest verifier."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import yaml

from examples.delivery import dogfood_bugfix as harness
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.work_contract import WorkWorkspace
from test_context_transport import SPEC

SOURCE = harness.FIXTURE.joinpath('task_queue.py').read_text()
FIXED = SOURCE.replace('self._pending = []', 'self._pending = []\n        self._sequence = 0').replace(
    '(priority, len(self._pending), task))', '(priority, self._sequence, task))\n        self._sequence += 1')


@unittest.skipIf(os.name=='nt', 'fake executables require POSIX shebang')
class BugfixDogfoodTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.bin=self.root/'bin';self.bin.mkdir()
        script=(Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        script=script.replace('outcome=plan.get', '''if state == 'REPRODUCE':
    import subprocess
    observed = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], capture_output=True)
    assert observed.returncode == 1
    plan['artifacts'][state] = [{'name':'reproduction_evidence','content':observed.stderr.decode()}]
if state == 'FIX':
    Path('task_queue.py').write_text('''+repr(FIXED)+''')
outcome=plan.get''')
        for name in ('claude','codex'):
            path=self.bin/name;path.write_text('#!'+sys.executable+'\n'+script);path.chmod(0o755)
        self.log=self.root/'calls.jsonl';self.plan=self.root/'plan.json'
        self.plan.write_text(json.dumps({'outcomes':{'ASSESS:0':'inconclusive','ASSESS:1':'diagnosis_disproved'},
            'artifacts':{'TRIAGE':[{'name':'bug_spec','content':SPEC}],
                         'DIAGNOSE':[{'name':'diagnosis','content':'Queue length reuses insertion order.'}],
                         'SPECIFY_FIX':[{'name':'fix_microspec','content':SPEC}],
                         'FIX':[{'name':'implementation_handoff','content':'Monotonic insertion counter.'}]}}))
        env={**os.environ,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.defpath,
             'FAKE_LOG':str(self.log),'FAKE_PLAN':str(self.plan)}
        self.config=ProviderRuntimeConfig(str(self.root),str(self.bin/'claude'),str(self.bin/'codex'),
            environment=env,model_aliases={'codex':{'codex':'offline-model'}},timeout_seconds=10)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_part_a_complete_bugfix_and_real_verifier(self):
        before=harness.inventory(harness.FIXTURE)
        report=harness.dogfood(self.config,'a',root=self.root/'a')
        self.assertEqual(report['terminal'],'READY_FOR_MANUAL_QA')
        self.assertEqual(report['state_sequence'],['TRIAGE','REPRODUCE','DIAGNOSE','SPECIFY_FIX','FIX','VERIFY','READY_FOR_MANUAL_QA'])
        self.assertEqual(report['changed_files'],['task_queue.py'])
        self.assertTrue(report['acceptance_tests_unchanged'])
        self.assertEqual(report['verification_outcomes'],['pass'])
        self.assertEqual(report['handoff_count'],5)
        self.assertEqual(harness.inventory(harness.FIXTURE),before)
        workspace=WorkWorkspace.open(self.root/'a/work')
        self.assertEqual(workspace.read_artifact('request'),harness.REQUEST)
        spec=yaml.safe_load((harness.REPO/'docs/plans/v2-006-bugfix-dogfood.yaml').read_text())
        self.assertEqual(harness.REQUEST,spec['planned_fixture']['human_report'])
        for name in ('bug_spec','reproduction_evidence','diagnosis','fix_microspec','verifier_evidence'):
            self.assertEqual(report['artifacts'][name],[1])
        self.assertIn('FAIL: test_fifo_after_remove_then_insert',workspace.read_artifact('reproduction_evidence'))
        self.assertIn('OK',workspace.read_artifact('verifier_evidence'))
        expected=[['repo_read'],['repo_read','command'],['repo_read','command'],['repo_read'],['repo_read','repo_write','command']]
        self.assertEqual([c['binding']['capabilities'] for c in self.calls()],expected)
        repro=self.calls()[1]['argv'];tools=repro[repro.index('--tools')+1].split(',')
        self.assertEqual(tools,['Read','Glob','Grep','Bash'])

    def test_part_b_actual_pipeline_and_lineage(self):
        report=harness.dogfood(self.config,'b',root=self.root/'b')
        self.assertEqual(report['terminal'],'ASSESSED')
        self.assertEqual(report['handoff_count'],3)
        calls=self.calls();self.assertEqual([c['provider'] for c in calls],['claude','claude','codex'])
        for i,(call,outcome) in enumerate(zip(calls,['inconclusive','diagnosis_disproved','ready'])):
            self.assertEqual(call['binding']['capabilities'],[])
            contract=json.loads(call['input'].split('STAGE OUTPUT CONTRACT\n')[1].split('\n\n')[0])
            self.assertEqual(contract['allowed_outcomes'],[outcome])
            facts=json.loads(call['input'].split('DURABLE INPUT ARTIFACTS\n')[1].split('\n\n')[0])
            self.assertEqual([f['name'] for f in facts],['request','reproduction_evidence'])
            self.assertEqual([f['version'] for f in facts],[1,1])
            observation=report['provider_invocations'][i]
            self.assertEqual([f['name'] for f in observation['inputs']],[f['name'] for f in facts])
            self.assertEqual('PRIOR REASONING HANDOFF' in call['input'],i==1)
            if i==1:
                handoff=json.loads(call['input'].split('PRIOR REASONING HANDOFF — advisory\n')[1].split('\n\n')[0])
                self.assertEqual(handoff['sequence'],1)
                self.assertEqual(observation['previous_handoff']['sequence'],1)
                self.assertEqual(handoff['rung_index'],0)
            else: self.assertIsNone(observation['previous_handoff'])
        for call in calls[:2]: self.assertEqual(call['argv'][call['argv'].index('--tools')+1],'')
        self.assertIn('features.shell_tool=false',calls[2]['argv'])
        self.assertEqual(calls[2]['argv'][calls[2]['argv'].index('--sandbox')+1],'read-only')
        self.assertEqual(report['changed_files'],[])

    def test_invalid_baselines_do_not_call_provider(self):
        for name,source in [('green',FIXED),('broken','raise RuntimeError("unrelated")')]:
            fixture=self.root/name;shutil.copytree(harness.FIXTURE,fixture)
            (fixture/'task_queue.py').write_text(source)
            root=self.root/('run-'+name)
            with patch.object(harness,'FIXTURE',fixture),self.assertRaises(harness.DogfoodFailure):
                harness.dogfood(self.config,'a',root=root)
            self.assertFalse(self.log.exists())
            self.assertTrue((root/'baseline-verification.txt').exists())
            self.assertEqual(json.loads((root/'report.json').read_text())['status'],'failed')

    def test_provider_failure_keeps_evidence_no_retry(self):
        config=replace(self.config,environment={**self.config.environment,'FAKE_MODE':'exit'})
        with self.assertRaises(harness.DogfoodFailure): harness.dogfood(config,'a',root=self.root/'failed')
        report=json.loads((self.root/'failed/report.json').read_text())
        self.assertEqual(len(self.calls()),1)
        self.assertEqual(report['artifacts'],{'request':[1]})
        self.assertEqual(report['handoff_count'],0)
        self.assertEqual(report['changed_files'],[])
        self.assertTrue((self.root/'failed/events.jsonl').exists())
        self.assertIn('run_error',report['event_kinds'])

    def test_acceptance_oracle_cannot_be_modified(self):
        path=self.bin/'claude'
        path.write_text(path.read_text().replace("if state == 'FIX':", "if state == 'FIX':\n    Path('tests/test_task_queue.py').write_text(Path('tests/test_task_queue.py').read_text()+'# changed oracle\\n')"))
        with self.assertRaises(harness.DogfoodFailure): harness.dogfood(self.config,'a',root=self.root/'tampered')
        report=json.loads((self.root/'tampered/report.json').read_text())
        self.assertTrue(any('acceptance tests changed' in e['message'] for e in report['errors']))
