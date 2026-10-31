"""CONTINUITY (#5) slice 1: adopt an old work's durable product into a new work on the same base."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from runtime import ExecutionError
from switchyard import continuity, recovery
from switchyard.continuity import adopt_v2_work
from switchyard.observability import read_events
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (execute_recoverable_v2_work, attach_qa_candidate, accept_v2_candidate, recovery_status,
                                 RecoveryBlocked)
from switchyard.suspend import suspend_v2_work
from switchyard.work_contract import WorkWorkspace

from test_delivery_isolation import Crash, crash_once, REQUEST
from test_delivery_suspend import SuspendBase, LOGO, NEW_BIN

ROOT = Path(__file__).resolve().parents[1]


class ContinuityBase(SuspendBase):
    def setUp(self):
        super().setUp()
        self.w2 = self.root/'w2'; self.d2 = self.w2/'workspace'

    def w1_ready(self, candidate=True, symlink=False):
        execution = self.start()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        d = self.delivery
        (d/'logo.png').write_bytes(LOGO[::-1]); (d/'obsolete.py').unlink()
        (d/'notes').mkdir(); (d/'notes/new.bin').write_bytes(NEW_BIN)
        (d/'tool.sh').write_text('#!/bin/sh\nexit 0\n'); (d/'tool.sh').chmod(0o755); (d/'script.sh').chmod(0o755)
        if symlink:
            (d/'link').symlink_to('keep.txt')
        if candidate:
            create_qa_candidate(d, execution.workspace, execution.result, 'feature', self.root/'qa1')
            attach_qa_candidate(self.work, self.root/'qa1')
        return execution

    def adopt(self, config=None):
        return adopt_v2_work(self.work, self.w2, self.text, config or self.config)

    def w1_bytes(self):
        """Every durable W1 byte (work root incl. its worktree files) plus its branch: must never change."""
        out = {str(p.relative_to(self.root)): p.read_bytes() for base in (self.work, self.root/'qa1') if base.exists()
               for p in sorted(base.rglob('*')) if p.is_file() and not p.is_symlink()}
        out['branch'] = self.git('rev-parse', 'switchyard/work').encode()
        if self.delivery.exists():
            out['status'] = self.git('status', '--porcelain=v2', '--untracked-files=all', cwd=self.delivery).encode()
        return out

    def files(self, d):
        facts = {}
        for p in sorted(d.rglob('*')):
            rel = p.relative_to(d)
            if '.git' in rel.parts:
                continue
            if p.is_symlink():
                facts[str(rel)] = ('link', os.readlink(p))
            elif p.is_file():
                facts[str(rel)] = (hashlib.sha256(p.read_bytes()).hexdigest(), os.access(p, os.X_OK))
        return facts

    def model_calls(self):
        return len(self.calls())


class AdoptionTests(ContinuityBase):
    # 2, 3, 4, 5, 6, 7, 8, 9, 11, 16-22
    def test_ready_candidate_is_adopted_into_a_new_verified_work_that_owns_its_lifecycle(self):
        self.w1_ready(); w1 = self.w1_bytes(); w1_status = recovery_status(self.work); w1_files = self.files(self.delivery)
        calls = self.model_calls(); self.verify_cwd.unlink()
        execution = self.adopt()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.model_calls(), calls)  # Zero model providers; only the current command verifier ran.
        self.assertEqual(self.verify_cwd.read_text(), str(self.d2))
        status = recovery_status(self.w2)
        self.assertEqual((status['work_id'], status['delivery_branch'], status['delivery_worktree'], status['base_sha']),
                         ('w2', 'refs/heads/switchyard/w2', str(self.d2), self.base))
        self.assertEqual((status['adopted'], status['adopted_from_work_id'], status['adopted_from_base_sha'],
                          status['adopted_implementation_source'], status['adoption_entry_state'], status['reused_request'],
                          status['reused_specification'], status['reused_implementation'], status['reused_artifacts']),
                         (True, 'work', self.base, 'candidate:1', 'VERIFY', True, True, True, ['implementation_handoff', 'microspec']))
        self.assertNotIn('qa_candidate', status)  # W1's candidate is provenance, not W2's lifecycle.
        self.assertEqual(self.files(self.d2), w1_files)  # Exact bytes, deletions, modes.
        self.assertFalse((self.d2/'obsolete.py').exists())
        self.assertEqual(self.git('rev-parse', 'switchyard/w2'), self.base)
        old, new = WorkWorkspace.open(self.work), WorkWorkspace.open(self.w2)
        for name in ('request', 'microspec', 'implementation_handoff'):
            self.assertEqual(new.read_artifact(name), old.read_artifact(name), name)
        self.assertEqual((len(new.versions('microspec')), len(new.versions('verifier_evidence'))), (1, 1))
        events = tuple(read_events(self.w2/'events.jsonl'))
        old_runs = {e.run_id for e in read_events(self.work/'events.jsonl')}
        self.assertTrue(old_runs.isdisjoint({e.run_id for e in events}))  # Fresh history, nothing copied.
        self.assertEqual([e.state.split(':')[2] for e in events if e.kind == 'state_entered' and e.state.startswith('@v2:')], ['VERIFY'])
        self.assertEqual(sum(e.kind == 'invocation_started' for e in events), 1)  # Fresh budgets: only VERIFY so far.
        self.assertEqual((self.w1_bytes(), recovery_status(self.work)), (w1, w1_status))
        # W2 produces its own candidate and acceptance; W1 stays as it was.
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        decision = accept_v2_candidate(self.w2)
        self.assertEqual(self.git('rev-parse', decision.committed_head+'^'), self.base)
        self.assertEqual(recovery_status(self.w2)['accepted_commit_sha'], decision.committed_head)
        self.assertEqual(self.git('rev-parse', 'switchyard/w2'), decision.committed_head)
        self.assertEqual((self.w1_bytes(), recovery_status(self.work)['terminal']), (w1, 'READY_FOR_MANUAL_QA'))

    # 1, 14, 23
    def test_accepted_work_is_adopted_from_its_commit_without_inheriting_acceptance(self):
        self.w1_ready(); commit = accept_v2_candidate(self.work).committed_head
        w1 = self.w1_bytes(); expected = self.files(self.delivery)
        self.adopt()
        status = recovery_status(self.w2)
        self.assertEqual((status['adopted_implementation_source'], status['terminal']), ('accepted_commit:'+commit, 'READY_FOR_MANUAL_QA'))
        self.assertNotIn('accepted_commit_sha', status)
        self.assertEqual(self.git('rev-parse', 'switchyard/w2'), self.base)  # W2 starts at A, not at W1's C.
        self.assertEqual(self.files(self.d2), expected)
        self.assertEqual((self.w1_bytes(), recovery_status(self.work)['terminal']), (w1, 'ACCEPTED'))

    # 10
    def test_suspended_shelf_is_adopted_without_resuming_the_old_work(self):
        self.w1_ready(candidate=False, symlink=True)
        self.git('add', 'tool.sh', cwd=self.delivery)  # Staged in W1; W2 starts with an unstaged implementation.
        expected = self.files(self.delivery)
        suspend_v2_work(self.work); w1 = self.w1_bytes()
        self.adopt()
        self.assertEqual(recovery_status(self.w2)['adopted_implementation_source'], 'shelf:1')
        self.assertEqual(self.files(self.d2), expected)
        self.assertEqual(os.readlink(self.d2/'link'), 'keep.txt')
        self.assertEqual(self.git('diff', '--cached', '--name-only', cwd=self.d2), '')
        self.assertFalse(self.delivery.exists())  # W1 was never resumed or recreated.
        self.assertEqual((self.w1_bytes(), recovery_status(self.work)['terminal']), (w1, 'SUSPENDED'))

    # 12, 13, 14, 27
    def test_uncertain_missing_changed_base_and_legacy_sources_are_refused_before_any_write(self):
        cases = []
        self.interrupted(); cases.append(('implementation_uncertain: interrupted', None))
        for reason, prepare in cases:
            with self.assertRaises(RecoveryBlocked) as ctx:
                self.adopt()
            self.assertEqual((ctx.exception.terminal, ctx.exception.reason), ('ADOPTION_BLOCKED', reason))
            self.assertFalse((self.w2/'recovery').exists())
        self.assertEqual(self.branches(), ['refs/heads/switchyard/work'])

    def test_ready_without_candidate_has_no_reusable_implementation(self):
        self.w1_ready(candidate=False); calls = self.model_calls()
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'no_reusable_implementation')
        self.assertFalse((self.w2/'recovery').exists()); self.assertEqual(self.model_calls(), calls)

    def test_changed_base_conflicting_with_the_implementation_is_refused(self):
        self.w1_ready(); w1 = self.w1_bytes(); calls = self.model_calls()
        (self.target/'source.py').write_text('B'); self.git('commit', '-qam', 'B')  # W1 also changed source.py.
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual((ctx.exception.terminal, ctx.exception.reason, ctx.exception.paths), ('ADOPTION_BLOCKED', 'transplant_conflict', ('source.py',)))
        self.assertFalse((self.w2/'recovery').exists()); self.assertEqual(self.branches(), ['refs/heads/switchyard/work'])
        self.assertEqual((self.w1_bytes(), self.model_calls()), (w1, calls))

    def test_legacy_and_unreadable_sources_are_unsupported(self):
        self.fail_once.write_text('1')
        with self.assertRaises(ExecutionError):
            execute_recoverable_v2_work(self.text, 'feature', REQUEST, self.work, self.config, isolation=False)
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'unsupported_source_schema: legacy in-place work')
        with self.assertRaises(RecoveryBlocked) as ctx:
            adopt_v2_work(self.root/'nothing-here', self.w2, self.text, self.config)
        self.assertEqual(ctx.exception.reason, 'unsupported_source_schema: no recovery baseline')

    def test_dirty_source_follows_the_isolation_policy(self):
        self.w1_ready(); (self.target/'stray.txt').write_text('dirty')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertTrue(ctx.exception.reason.startswith('dirty_source_worktree'))

    # 24, 26
    def test_crash_after_branch_creation_retries_into_one_work(self):
        self.w1_ready(); w1 = self.w1_bytes()
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.adopt()
        self.assertEqual(self.git('rev-parse', 'switchyard/w2'), self.base); self.assertFalse(self.d2.exists())
        with self.assertRaises(RecoveryBlocked) as ctx:
            recovery_status(self.w2)
        self.assertEqual(ctx.exception.reason, 'adoption_incomplete: retry the same adopt')
        self.assertEqual(self.w1_bytes(), w1)
        self.assertEqual(self.adopt().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(sorted(self.branches()), ['refs/heads/switchyard/w2', 'refs/heads/switchyard/work'])
        self.assertEqual(self.worktrees().count(str(self.d2)), 1)
        ops = [json.loads(WorkWorkspace.open(self.w2/'recovery').read_artifact('operation', r.version))
               for r in WorkWorkspace.open(self.w2/'recovery').versions('operation')]
        self.assertEqual([o['phase'] for o in ops].count('provisioning'), 1)  # One adoption record.

    # 25, 26
    def test_crash_during_materialization_rebuilds_exact_bytes(self):
        self.w1_ready(); w1 = self.w1_bytes(); expected = self.files(self.delivery)
        calls = {'n': 0}
        original = continuity._write_change
        def write(*args):
            calls['n'] += 1
            if calls['n'] == 3:
                raise Crash()
            return original(*args)
        with patch('switchyard.continuity._write_change', write), self.assertRaises(Crash):
            self.adopt()
        (self.d2/'debris.tmp').write_text('partial')
        self.assertEqual(self.w1_bytes(), w1)
        self.assertEqual(self.adopt().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.files(self.d2), expected)

    def test_cli_adopt(self):
        self.w1_ready()
        out = subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'adopt', str(self.work), str(self.w2),
                              '--claude-executable', str(self.root/'claude'), '--codex-executable', str(self.root/'codex')], cwd=ROOT,
                             capture_output=True, text=True, env={**self.config.environment})
        self.assertEqual(out.returncode, 0, out.stderr)
        status = json.loads(out.stdout)
        self.assertEqual((status['terminal'], status['adopted_implementation_source']), ('READY_FOR_MANUAL_QA', 'candidate:1'))


if __name__ == '__main__':
    unittest.main()
