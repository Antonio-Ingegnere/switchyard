"""Offline runner proof: fake model processes, actual unittest command verification."""
from dataclasses import replace, FrozenInstanceError
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from examples.delivery import dogfood_feature as harness
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.work_contract import WorkWorkspace
from switchyard.workflow_bundle import load_workflow_bundle
from test_context_transport import SPEC


FIX = '''"""Exact catalog lookup with optional case-insensitive comparison."""
ITEMS = {"Notebook": 12, "Pencil": 3, "Desk Lamp": 25}
def lookup(name, *, ignore_case=False):
    if ignore_case:
        return next((price for key, price in ITEMS.items() if key.casefold() == name.casefold()), None)
    return ITEMS.get(name)
'''


@unittest.skipIf(os.name == 'nt', 'fake executable requires POSIX shebang')
class FeatureDogfoodTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root/'bin'; self.bin.mkdir()
        script = (Path(__file__).parent/'fixtures/provider_cli.py').read_text()
        script = script.replace("outcome=plan.get", "if state == 'IMPLEMENT':\n    Path('catalog.py').write_text("+repr(FIX)+")\noutcome=plan.get")
        for name in ('claude', 'codex'):
            path = self.bin/name
            path.write_text('#!'+sys.executable+'\n'+script); path.chmod(0o755)
        self.log = self.root/'calls.jsonl'
        plan = self.root/'plan.json'
        plan.write_text(json.dumps({'artifacts': {
            'SPECIFY': [{'name':'microspec','content':SPEC}],
            'IMPLEMENT': [{'name':'implementation_handoff','content':'Implemented optional exact lookup.'}],
        }}))
        env = {**os.environ, 'PATH':str(Path(sys.executable).parent)+os.pathsep+os.defpath,
               'FAKE_LOG':str(self.log), 'FAKE_PLAN':str(plan)}
        self.config = ProviderRuntimeConfig(str(self.root), str(self.bin/'claude'), str(self.bin/'codex'),
                                            environment=env, timeout_seconds=5,
                                            model_aliases={'codex':{'codex':'offline-model'}})

    def test_offline_complete_feature_and_real_verifier(self):
        fixture_before = harness.inventory(harness.FIXTURE)
        root = self.root/'dogfood'
        report = harness.dogfood(self.config, root=root)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['terminal'], 'READY_FOR_MANUAL_QA')
        self.assertEqual(report['state_sequence'], ['SPECIFY','IMPLEMENT','VERIFY','READY_FOR_MANUAL_QA'])
        self.assertEqual(report['verification_outcomes'], ['pass'])
        self.assertEqual(report['changed_files'], ['catalog.py'])
        self.assertEqual(report['handoff_count'], 2)
        self.assertFalse(report['escalation_occurred'])
        self.assertEqual(harness.inventory(harness.FIXTURE), fixture_before)
        workspace = WorkWorkspace.open(root/'work')
        self.assertEqual(workspace.read_artifact('request'), harness.REQUEST)
        self.assertEqual(workspace.read_artifact('microspec'), SPEC)
        self.assertIn('OK', workspace.read_artifact('verifier_evidence'))
        self.assertEqual(report['artifacts']['verifier_evidence'], [1])
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([call['binding']['logical_state'] for call in calls], ['SPECIFY','IMPLEMENT'])
        for call in calls:
            self.assertEqual(Path(call['cwd']).resolve(), (root/'target').resolve())
        self.assertIn(SPEC, json.loads(calls[1]['input'].split('DURABLE INPUT ARTIFACTS\n')[1].split('\n\n')[0])[1]['content'])
        events = [json.loads(line) for line in (root/'events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e['kind']=='invocation_started' for e in events), 3)
        self.assertTrue(all(e['duration_ns'] is not None for e in events if e['kind']=='invocation_finished'))

    def test_failed_provider_preserves_evidence_without_retry(self):
        config = replace(self.config, environment={**self.config.environment, 'FAKE_MODE':'exit'})
        root = self.root/'failed'
        with self.assertRaises(harness.DogfoodFailure): harness.dogfood(config, root=root)
        report = json.loads((root/'report.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['invocations_started'], 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertEqual(report['changed_files'], [])
        self.assertTrue((root/'work/request.md').exists())
        self.assertTrue((root/'events.jsonl').exists())
        self.assertTrue(any('status 7' in error['message'] for error in report['errors']))

    def test_bad_baseline_stops_before_provider(self):
        for mode in ('green', 'unrelated'):
            with self.subTest(mode=mode):
                fixture = self.root/('fixture-'+mode)
                shutil.copytree(harness.FIXTURE, fixture)
                (fixture/'catalog.py').write_text(FIX if mode == 'green' else 'raise RuntimeError("broken fixture")\n')
                with patch.object(harness, 'FIXTURE', fixture), self.assertRaises(harness.DogfoodFailure):
                    harness.dogfood(self.config, root=self.root/('run-'+mode))
                self.assertFalse(self.log.exists())

    def test_runner_returns_small_immutable_references(self):
        from switchyard.workflow_execution import execute_v2_workflow
        from runtime.recording import JsonlRecorder
        target = self.root/'target'; shutil.copytree(harness.FIXTURE, target)
        config = replace(self.config, working_directory=str(target))
        bundle = load_workflow_bundle(harness.BUNDLE)
        execution = execute_v2_workflow(bundle, 'feature', harness.REQUEST, self.root/'work', config,
                                        recorder=JsonlRecorder(self.root/'events.jsonl'))
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(execution.workspace.root, self.root/'work')
        self.assertEqual(len(execution.compiled.executors), 8)
        with self.assertRaises(FrozenInstanceError): execution.result = None


if __name__ == '__main__':
    unittest.main()
