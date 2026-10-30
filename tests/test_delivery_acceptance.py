"""DELIVERY-ISOLATION (#3) acceptance: exactly one durable accepted commit on the delivery branch."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from switchyard import qa_lifecycle, recovery
from switchyard.git_safety import ScopeBlocked
from switchyard.qa_lifecycle import create_qa_candidate, accept_v2_work, LifecycleError
from switchyard.recovery import (execute_recoverable_v2_work, attach_qa_candidate, accept_v2_candidate, qa_fix_v2_work,
                                 recovery_status, rollback_v2_work, qa_history, RecoveryBlocked)
from switchyard.work_contract import WorkWorkspace

from test_delivery_isolation import IsolationBase, Crash, crash_once, REQUEST

ROOT = Path(__file__).resolve().parents[1]


def crash_after(original):
    """The wrapped Git step completes, then the controller dies."""
    state = {'done': False}
    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if not state['done']:
            state['done'] = True
            raise Crash()
        return result
    return wrapper


class AcceptanceBase(IsolationBase):
    def ready(self):
        execution = self.start()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.qa = self.root/'qa'
        self.first = create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.qa)
        attach_qa_candidate(self.work, self.qa)
        return self.first

    def dgit(self, *args):
        return self.git(*args, cwd=self.delivery)

    def acceptance_commits(self):
        return self.git('rev-list', self.base+'..switchyard/work').split()

    def store_op(self):
        return json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('operation'))

    def index_lock(self):
        path = Path(self.git('rev-parse', '--git-path', 'index', cwd=self.delivery)+'.lock')
        return path if path.is_absolute() else self.delivery/path

    def hard_exit_leftovers(self, candidate_workspace):
        # A real process death leaves the O_EXCL lock files that `finally` would have removed.
        (Path(candidate_workspace)/'.lifecycle.lock').write_text('')
        self.index_lock().write_text('')

    def assert_accepted_once(self, commit, identity, sequence=1):
        self.assertEqual(self.acceptance_commits(), [commit])
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), commit)
        self.assertEqual(self.git('rev-parse', commit+'^'), self.base)
        self.assertEqual(self.dgit('status', '--porcelain', '--untracked-files=all'), '')
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['accepted_commit_sha'], status['accepted_candidate_identity'],
                          status['accepted_candidate_sequence'], status['delivery_branch'], status['base_sha']),
                         ('ACCEPTED', commit, identity, sequence, 'refs/heads/switchyard/work', self.base))
        self.assertNotIn('acceptance_finalization_pending', status)
        op = self.store_op()
        self.assertEqual((op['phase'], op['accepted_commit_sha'], op['accepted_candidate_identity']), ('accepted', commit, identity))
        message = self.git('log', '-1', '--format=%B', commit)
        self.assertIn('Switchyard-Work: work', message); self.assertIn('Switchyard-Candidate: '+identity, message)
        self.assertIn('Switchyard-Candidate-Sequence: %d' % sequence, message); self.assertNotIn(REQUEST, message)


class IsolatedAcceptanceTests(AcceptanceBase):
    # 1, 2, 3, 4, 6, 12, 15, 17, 20
    def test_direct_accept_commits_exactly_the_candidate_on_the_delivery_branch(self):
        first = self.ready(); before = self.source_state()
        decision = accept_v2_candidate(self.work)
        self.assertEqual(decision.terminal, 'ACCEPTED')
        commit = decision.committed_head; self.assertEqual(len(commit), 40)
        self.assert_accepted_once(commit, first.identity)
        self.assertEqual(self.git('diff', '--name-only', self.base, commit), 'source.py')
        self.assertEqual(self.git('show', commit+':source.py'), 'changed')
        self.assertEqual(self.source_state(), before)  # Source branch, HEAD, index, files, status.
        # A fresh process (restart) finds the accepted commit in durable work state, not via branch HEAD.
        out = subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'status', str(self.work)], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)['accepted_commit_sha'], commit)
        # Retrying never commits again or moves anything.
        for again in (lambda: accept_v2_candidate(self.work), lambda: qa_fix_v2_work(self.work, 'more', self.text, self.config),
                      lambda: rollback_v2_work(self.work)):
            with self.assertRaises(RecoveryBlocked):
                again()
        self.assert_accepted_once(commit, first.identity)

    # 5, 11
    def test_main_advancing_before_accept_keeps_the_commit_on_the_recorded_base(self):
        first = self.ready()
        (self.target/'source.py').write_text('B'); self.git('commit', '-qam', 'B'); b = self.git('rev-parse', 'HEAD')
        before = self.source_state()
        commit = accept_v2_candidate(self.work).committed_head
        self.assert_accepted_once(commit, first.identity)
        self.assertEqual((self.git('rev-parse', 'main'), self.git('rev-parse', 'HEAD')), (b, b))
        self.assertEqual(self.git('rev-parse', commit+'^'), self.base)  # A -> C, never B -> C.
        self.assertEqual(self.source_state(), before)

    # 7
    def test_worktree_drift_after_candidate_is_refused_before_any_acceptance_record(self):
        self.ready()
        for label, drift, undo in (
                ('extra file', lambda: (self.delivery/'stray.txt').write_text('later'), lambda: (self.delivery/'stray.txt').unlink()),
                ('content', lambda: (self.delivery/'source.py').write_text('edited later'), lambda: (self.delivery/'source.py').write_text('changed'))):
            with self.subTest(label):
                drift()
                with self.assertRaises(RecoveryBlocked) as ctx:
                    accept_v2_candidate(self.work)
                self.assertTrue(ctx.exception.reason.startswith('candidate_worktree_drift: '), ctx.exception.reason)
                self.assertEqual(WorkWorkspace.open(self.qa).versions('qa_intent'), ())
                self.assertEqual(self.acceptance_commits(), [])
                self.assertEqual(recovery_status(self.work)['terminal'], 'READY_FOR_MANUAL_QA')
                undo()
        self.assert_accepted_once(accept_v2_candidate(self.work).committed_head, self.first.identity)

    # 10
    def test_crash_before_the_commit_retries_into_one_commit(self):
        first = self.ready()
        with patch.object(qa_lifecycle._Git, 'commit_tree', crash_once(qa_lifecycle._Git.commit_tree)), self.assertRaises(Crash):
            accept_v2_candidate(self.work)
        self.hard_exit_leftovers(self.qa)
        self.assertEqual(self.acceptance_commits(), [])
        self.assertEqual(recovery_status(self.work)['terminal'], 'READY_FOR_MANUAL_QA')
        self.assert_accepted_once(accept_v2_candidate(self.work).committed_head, first.identity)

    def test_crash_after_commit_object_before_publication_retries_into_one_commit(self):
        first = self.ready()
        with patch.object(qa_lifecycle._Git, 'advance_branch', crash_once(qa_lifecycle._Git.advance_branch)), self.assertRaises(Crash):
            accept_v2_candidate(self.work)
        self.hard_exit_leftovers(self.qa)
        self.assertEqual(self.acceptance_commits(), [])  # Prepared, never published.
        self.assert_accepted_once(accept_v2_candidate(self.work).committed_head, first.identity)

    # 11, 12
    def test_crash_after_published_commit_reuses_it(self):
        first = self.ready()
        with patch.object(qa_lifecycle._Git, 'advance_branch', crash_after(qa_lifecycle._Git.advance_branch)), self.assertRaises(Crash):
            accept_v2_candidate(self.work)
        self.hard_exit_leftovers(self.qa)
        published = self.git('rev-parse', 'switchyard/work')
        intents = [json.loads(WorkWorkspace.open(self.qa).read_artifact('qa_intent', r.version)) for r in WorkWorkspace.open(self.qa).versions('qa_intent')]
        self.assertEqual(intents[-1], {'candidate_identity': first.identity, 'decision': 'accept', 'phase': 'prepared', 'commit': published})
        status = recovery_status(self.work)  # Not final: no ACCEPTED without a durable commit record.
        self.assertEqual(status['terminal'], 'READY_FOR_MANUAL_QA'); self.assertNotIn('accepted_commit_sha', status)
        decision = accept_v2_candidate(self.work)
        self.assertEqual(decision.committed_head, published)
        self.assert_accepted_once(published, first.identity)

    def test_crash_after_decision_before_work_record_finishes_without_git(self):
        first = self.ready()
        with patch('switchyard.recovery._accepted', crash_once(recovery._accepted)), self.assertRaises(Crash):
            accept_v2_candidate(self.work)
        commit = self.git('rev-parse', 'switchyard/work')
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['accepted_commit_sha'], status['acceptance_finalization_pending']),
                         ('ACCEPTED', commit, True))
        with patch.object(qa_lifecycle._Git, 'commit_tree', side_effect=AssertionError('no Git')):
            self.assertEqual(accept_v2_candidate(self.work).committed_head, commit)
        self.assert_accepted_once(commit, first.identity)

    # 13
    def test_ambiguous_branch_head_after_crash_is_refused_never_reset(self):
        self.ready()
        with patch.object(qa_lifecycle._Git, 'advance_branch', crash_after(qa_lifecycle._Git.advance_branch)), self.assertRaises(Crash):
            accept_v2_candidate(self.work)
        self.hard_exit_leftovers(self.qa)
        self.index_lock().unlink(missing_ok=True)  # Someone else then committed on the delivery branch.
        self.dgit('read-tree', 'HEAD'); self.dgit('commit', '-q', '--allow-empty', '-m', 'someone else')
        other = self.git('rev-parse', 'switchyard/work')
        with self.assertRaises(RecoveryBlocked) as ctx:
            accept_v2_candidate(self.work)
        self.assertTrue(ctx.exception.reason.startswith('accept_reconciliation_ambiguous'), ctx.exception.reason)
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), other)
        self.assertEqual(WorkWorkspace.open(self.qa).versions('qa_decision'), ())
        self.assertNotEqual(recovery_status(self.work)['terminal'], 'ACCEPTED')

    # 14
    def test_git_commit_failure_never_exposes_accepted(self):
        self.ready()
        with patch.object(qa_lifecycle._Git, 'commit_tree', side_effect=ScopeBlocked('Git inspection or update failed')):
            decision = accept_v2_candidate(self.work)
        self.assertEqual((decision.terminal, decision.reason, decision.committed_head), ('COMMIT_BLOCKED', 'commit_failed', None))
        status = recovery_status(self.work)
        self.assertEqual(status['terminal'], 'COMMIT_BLOCKED'); self.assertNotIn('accepted_commit_sha', status)
        self.assertEqual(self.acceptance_commits(), []); self.assertNotEqual(self.store_op()['phase'], 'accepted')


class CorrectiveAcceptanceTests(AcceptanceBase):
    def setUp(self):
        super().setUp()
        flow = self.data['flows']['feature']['states']
        flow['IMPLEMENT']['budget'] = flow['VERIFY']['budget'] = {'visits': 3}
        self.text = json.dumps(self.data)

    # 8, 16, 18
    def test_multi_round_accepts_candidate_three_only_and_verifies_findings(self):
        first = self.ready()
        qa_fix_v2_work(self.work, 'Round one finding.', self.text, self.config)
        second = recovery_status(self.work)['qa_candidate']
        qa_fix_v2_work(self.work, 'Round two finding.', self.text, self.config)
        third = recovery_status(self.work)['qa_candidate']
        self.assertEqual(third['sequence'], 3)
        for old in (self.qa, second['workspace']):  # Rejected/superseded candidates cannot be committed.
            with self.assertRaisesRegex(LifecycleError, 'already finalized'):
                accept_v2_work(self.delivery, old)
        self.assertEqual(self.acceptance_commits(), [])
        before = self.source_state()
        commit = accept_v2_candidate(self.work).committed_head
        self.assert_accepted_once(commit, third['identity'], sequence=3)
        self.assertNotIn(first.identity, self.git('log', '-1', '--format=%B', commit))
        self.assertEqual(self.source_state(), before)
        statuses = [f['status'] for r in qa_history(self.work)['rounds'] for f in r['findings']]
        self.assertEqual(statuses, ['verified', 'verified'])

    # 9
    def test_open_qa_round_prevents_acceptance(self):
        self.ready()
        with patch('switchyard.recovery._complete_qa_fix', crash_once(recovery._complete_qa_fix)), self.assertRaises(Crash):
            qa_fix_v2_work(self.work, 'Pending finding.', self.text, self.config)
        with self.assertRaises(RecoveryBlocked) as ctx:
            accept_v2_candidate(self.work)
        self.assertEqual(ctx.exception.reason, 'corrective_cycle_open')
        self.assertEqual(self.acceptance_commits(), [])


class LegacyAcceptanceTests(IsolationBase):
    # 19
    def test_legacy_in_place_acceptance_is_unchanged(self):
        execution = execute_recoverable_v2_work(self.text, 'feature', REQUEST, self.work, self.config, isolation=False)
        create_qa_candidate(self.target, execution.workspace, execution.result, 'feature', self.root/'qa')
        attach_qa_candidate(self.work, self.root/'qa')
        decision = accept_v2_candidate(self.work)
        self.assertEqual(decision.terminal, 'ACCEPTED')
        self.assertEqual(self.git('rev-parse', 'main'), decision.committed_head)  # In place, as before.
        self.assertEqual(self.git('log', '-1', '--format=%B'), 'feature: accept reviewed V2 work')
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['isolated']), ('ACCEPTED', False))
        self.assertNotIn('accepted_commit_sha', status); self.assertEqual(self.branches(), [])


if __name__ == '__main__':
    unittest.main()
