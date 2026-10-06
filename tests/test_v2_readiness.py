"""Complete V2 delivery/QA across processes with fake CLIs and actual verifier/Git."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from examples.delivery.offline_lifecycle import dogfood, FEEDBACK
from switchyard.work_contract import WorkWorkspace


@unittest.skipIf(os.name == 'nt', 'fake executable requires POSIX shebang')
class ReadinessTests(unittest.TestCase):
    def test_feature_accept_after_process_restart(self):
        with TemporaryDirectory() as tmp:
            report = dogfood(Path(tmp)/'accept', 'accept')
            self.assertEqual(report['terminal'], 'ACCEPTED')
            self.assertNotEqual(report['delivery_pid'], report['lifecycle_pid'])
            self.assertEqual(report['provider_calls_during_qa'], 0)
            self.assertEqual(report['changed_paths'], ['catalog.py'])

    def test_feature_reopen_after_process_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)/'reopen'
            report = dogfood(root, 'reopen')
            self.assertEqual(report['terminal'], 'REOPENED')
            self.assertEqual(report['base_head'], report['final_head'])
            workspace = WorkWorkspace.open(root/'qa')
            self.assertEqual(workspace.read_artifact('qa_feedback'), FEEDBACK)
            self.assertEqual(workspace.latest_artifact('qa_candidate').sha256, report['candidate_identity'])
            self.assertEqual(workspace.read_artifact('request'), WorkWorkspace.open(root/'work').read_artifact('request'))


if __name__ == '__main__':
    unittest.main()
