"""DELIVERY-SUSPEND (#4) slice 2: SUSPENDED -> recreate the same worktree -> exact shelf restore -> same work."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from runtime import ExecutionError
from switchyard import suspend
from switchyard.context_transport import StageResult, StageHandoff
from switchyard.observability import read_events
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (attach_qa_candidate, accept_v2_candidate, qa_fix_v2_work, resume_v2_work, answer_v2_work,
                                 recovery_status, qa_history, RecoveryBlocked)
from switchyard.suspend import suspend_v2_work, verify_shelf
from switchyard.work_contract import WorkWorkspace

from test_delivery_isolation import Crash
from test_delivery_suspend import SuspendBase

ROOT = Path(__file__).resolve().parents[1]


def crash_after(original, nth=1):
    state = {'n': 0}
    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        state['n'] += 1
        if state['n'] == nth:
            raise Crash()
        return result
    return wrapper


def crash_on(original, nth):
    state = {'n': 0}
    def wrapper(*args, **kwargs):
        state['n'] += 1
        if state['n'] == nth:
            raise Crash()
        return original(*args, **kwargs)
    return wrapper


class RestoreBase(SuspendBase):
    def setUp(self):
        super().setUp()
        states = self.data['flows']['feature']['states']
        states['IMPLEMENT']['budget'] = states['VERIFY']['budget'] = {'visits': 3}
        states['VERIFY']['rung_budget_scope'] = 'visit'
        self.text = json.dumps(self.data)
        # The failing IMPLEMENT attempt leaves every kind of partial work (and staging) behind, so the
        # interruption checkpoint itself is dirty and ordinary resume's drift check proves exactness.
        partial = '''        if os.environ.get('LEAVE_PARTIAL'):
            import subprocess
            Path('source.py').write_text('partial text\\n')
            Path('logo.png').write_bytes(bytes(range(256))[::-1]*4)
            Path('obsolete.py').unlink()
            Path('notes').mkdir(); Path('notes/new.bin').write_bytes(b'\\x00\\xffpartial\\x80')
            Path('script.sh').chmod(0o755)
            Path('tool.sh').write_text('#!/bin/sh\\nexit 0\\n'); Path('tool.sh').chmod(0o755)
            Path('link').symlink_to('keep.txt')
            subprocess.run(['git', 'add', 'tool.sh'], check=True)
            Path('keep.txt').write_text('staged\\n'); subprocess.run(['git', 'add', 'keep.txt'], check=True)
            Path('keep.txt').write_text('unstaged on top of staged\\n')
        flag.unlink(); sys.exit(7)'''
        exe = self.root/'claude'; text = exe.read_text()
        exe.write_text(text.replace('        flag.unlink(); sys.exit(7)', partial))

    def dirty_staged(self):
        self.dirty()
        self.git('add', 'tool.sh', cwd=self.delivery)  # Meaningful staged state: a new executable.
        (self.delivery/'keep.txt').write_text('staged\n'); self.git('add', 'keep.txt', cwd=self.delivery)
        (self.delivery/'keep.txt').write_text('unstaged on top of staged\n')

    def picture(self):
        """Everything restore must reproduce: bytes, permissions, links, Git status v2 and the index."""
        facts, _ = self.worktree_facts()
        return dict(facts=facts, status=self.git('status', '--porcelain=v2', '--untracked-files=all', cwd=self.delivery),
                    index=self.git('ls-files', '--stage', cwd=self.delivery), head=self.git('rev-parse', 'HEAD', cwd=self.delivery),
                    branch=self.git('symbolic-ref', 'HEAD', cwd=self.delivery),
                    admin=self.git('rev-parse', '--absolute-git-dir', cwd=self.delivery))

    def partial_interruption(self):
        self.config = self.provider(self.target)
        self.config = type(self.config)(self.config.working_directory, self.config.claude_executable, self.config.codex_executable,
                                        environment={**self.config.environment, 'LEAVE_PARTIAL': '1'})
        self.interrupted()
        self.assertEqual(self.git('diff', '--cached', '--name-only', cwd=self.delivery).split(), ['keep.txt', 'tool.sh'])

    def events(self):
        return (self.work/'events.jsonl').read_bytes()

    def advance_source(self):
        for text in ('B', 'D'):
            (self.target/'source.py').write_text(text); self.git('commit', '-qam', text)
        (self.target/'scratch.txt').write_text('user dirt')  # Source dirt must not matter either.
        return self.source_state()


class RestoreTests(RestoreBase):
    # 1-4, 6-16, 20, 27
    def test_ready_for_qa_restores_the_exact_workspace_without_providers(self):
        self.assertEqual(self.start().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.dirty_staged(); before = self.picture(); calls = len(self.calls()); events = self.events(); durable = self.durable()
        suspend_v2_work(self.work, 'lunch')
        self.assertFalse(self.delivery.exists())
        source = self.advance_source()
        status = resume_v2_work(self.work, self.text, self.config)
        self.assertEqual((status['work_id'], status['terminal'], status.get('suspended', False), status['delivery_worktree_state'], status['delivery_branch']),
                         ('work', 'READY_FOR_MANUAL_QA', False, 'present', 'refs/heads/switchyard/work'))
        self.assertNotIn('isolation_blocked', status)
        self.assertEqual(self.picture(), before)  # Same path, branch, HEAD, admin dir, bytes, modes, links, status, index.
        self.assertEqual((len(self.calls()), self.events(), self.durable()), (calls, events, durable))  # No provider, no budget.
        self.assertEqual(self.source_state(), source)
        self.assertEqual(self.worktrees().count(str(self.delivery)), 1)
        op = self.ops()[-1]
        self.assertEqual((op['phase'], op['terminal'], op['restored_shelf']), ('delivery_done', 'READY_FOR_MANUAL_QA', 'shelf-1'))
        self.assertTrue((self.shelf()/'manifest.json').is_file())  # Shelf retained.
        self.assertEqual(os.readlink(self.delivery/'link'), 'keep.txt')
        self.assertTrue(os.access(self.delivery/'tool.sh', os.X_OK)); self.assertTrue(os.access(self.delivery/'script.sh', os.X_OK))

    # 18, 20
    def test_interrupted_restores_then_follows_ordinary_resume(self):
        self.partial_interruption(); picture = self.picture()
        before = [e for e in read_events(self.work/'events.jsonl') if e.kind == 'invocation_started']
        suspend_v2_work(self.work)
        self.assertEqual(recovery_status(self.work)['workflow_state'], 'IMPLEMENT')
        execution = resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        events = tuple(read_events(self.work/'events.jsonl'))
        started = [e for e in events if e.kind == 'invocation_started']
        self.assertEqual(started[:len(before)], before)  # History continues; nothing reset.
        self.assertEqual([e.state.split(':')[2] for e in started[len(before):]], ['IMPLEMENT', 'VERIFY'])
        self.assertEqual(len({e.run_id for e in events}), 1)
        self.assertEqual(self.picture()['facts']['notes/new.bin'], picture['facts']['notes/new.bin'])  # Partial work kept.
        self.assertEqual(os.readlink(self.delivery/'link'), 'keep.txt')
        self.assertEqual({c['cwd'] for c in self.calls()}, {str(self.delivery)})

    def test_restore_is_exact_and_ordinary_resume_rules_still_apply(self):
        self.interrupted(); (self.delivery/'human.txt').write_text('edited after the checkpoint')
        suspend_v2_work(self.work)
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual((ctx.exception.reason, ctx.exception.paths), ('checkpoint_drift', ('human.txt',)))
        status = recovery_status(self.work)  # Restored (same as before suspend), then refused like before.
        self.assertEqual((status['terminal'], status['restored_shelf'] if 'restored_shelf' in status else self.ops()[-1]['restored_shelf']),
                         ('INTERRUPTED', 'shelf-1'))
        self.assertEqual((self.delivery/'human.txt').read_text(), 'edited after the checkpoint')

    # 5
    def test_moved_delivery_branch_blocks_resume_and_stays_suspended(self):
        self.interrupted(); suspend_v2_work(self.work)
        other = self.git('commit-tree', self.base+'^{tree}', '-p', self.base, '-m', 'moved')
        self.git('update-ref', 'refs/heads/switchyard/work', other)
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(ctx.exception.reason, 'delivery_branch_moved')
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), other)  # Never reset.
        self.assertFalse(self.delivery.exists()); self.assertEqual(recovery_status(self.work)['terminal'], 'SUSPENDED')

    # 25, 26
    def test_corrupt_shelf_or_missing_git_blob_refuses_before_any_worktree(self):
        self.partial_interruption(); suspend_v2_work(self.work)
        manifest = json.loads((self.shelf()/'manifest.json').read_bytes())
        blob = self.shelf()/'blobs'/manifest['files']['logo.png']['blob']; good = blob.read_bytes()
        blob.write_bytes(good[:-1]+b'X')
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual((ctx.exception.reason, ctx.exception.paths), ('shelf_blob_corrupt', ('logo.png',)))
        blob.write_bytes(good)
        oid = manifest['files']['.gitignore']['git_blob']
        loose = self.target/'.git/objects'/oid[:2]/oid[2:]; saved = loose.read_bytes(); loose.chmod(0o644); loose.unlink()
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual((ctx.exception.reason, ctx.exception.paths), ('shelf_git_blob_missing', ('.gitignore',)))
        self.assertFalse(self.delivery.exists())
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['resume_allowed']), ('SUSPENDED', True))  # Still recoverable.
        loose.write_bytes(saved)
        self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')

    # 24
    def test_conflicting_path_or_checkout_is_refused_without_deletion(self):
        self.interrupted(); suspend_v2_work(self.work)
        self.delivery.mkdir(); (self.delivery/'mine.txt').write_text('user data')
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(ctx.exception.reason, 'delivery_restore_path_conflict')
        self.assertEqual((self.delivery/'mine.txt').read_text(), 'user data')
        shutil.rmtree(self.delivery)
        self.git('worktree', 'add', '-q', str(self.root/'elsewhere'), 'switchyard/work')
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertTrue(ctx.exception.reason.startswith('delivery_restore_worktree_conflict'), ctx.exception.reason)
        self.assertTrue((self.root/'elsewhere').is_dir()); self.assertEqual(recovery_status(self.work)['terminal'], 'SUSPENDED')

    # 21
    def test_crash_after_worktree_creation_retries_onto_the_same_worktree(self):
        self.partial_interruption(); before = self.picture(); suspend_v2_work(self.work)
        with patch('switchyard.suspend._add_worktree', crash_after(suspend._add_worktree)), self.assertRaises(Crash):
            resume_v2_work(self.work, self.text, self.config)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['restore_pending'], status['suspended'], status['delivery_worktree_state']),
                         ('RESTORING', True, True, 'restoring'))
        self.assertNotIn('isolation_blocked', status)
        with patch('switchyard.recovery._drive', side_effect=AssertionError('restore only checked here')), self.assertRaises(AssertionError):
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(self.picture(), before)
        self.assertEqual(self.worktrees().count(str(self.delivery)), 1)

    # 22
    def test_crash_midway_through_file_restoration_rebuilds_from_head_and_shelf(self):
        self.partial_interruption(); before = self.picture(); suspend_v2_work(self.work)
        with patch('switchyard.suspend._write_entry', crash_on(suspend._write_entry, 4)), self.assertRaises(Crash):
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(recovery_status(self.work)['terminal'], 'RESTORING')
        (self.delivery/'half-written.tmp').write_text('debris')  # Partial state must never be trusted.
        with patch('switchyard.recovery._drive', side_effect=AssertionError('stop')), self.assertRaises(AssertionError):
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(self.picture(), before)

    # 23
    def test_crash_after_verification_before_final_state_runs_providers_once(self):
        self.interrupted(); suspend_v2_work(self.work); calls = len(self.calls())
        with patch('switchyard.suspend._finalize', crash_on(suspend._finalize, 1)), self.assertRaises(Crash):
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual((recovery_status(self.work)['terminal'], len(self.calls())), ('RESTORING', calls))
        with patch('switchyard.suspend._rebuild', side_effect=AssertionError('verified workspace is reused, not rebuilt')):
            self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual([c['binding']['logical_state'] for c in self.calls()[calls:]], ['IMPLEMENT'])  # Exactly once.
        verify = [e for e in read_events(self.work/'events.jsonl') if e.kind == 'invocation_started' and 'VERIFY' in e.state]
        self.assertEqual(len(verify), 1)

    def test_unfinished_cleanup_adopts_the_identical_worktree(self):
        self.partial_interruption(); before = self.picture()
        with patch('switchyard.suspend._remove_worktree', side_effect=Crash()), self.assertRaises(Crash):
            suspend_v2_work(self.work)
        with patch('switchyard.recovery._drive', side_effect=AssertionError('stop')), self.assertRaises(AssertionError):
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(self.picture(), before)


class RestoreAdminNameTests(RestoreBase):
    def test_recreated_worktree_takes_back_its_recorded_git_admin_name(self):
        from switchyard.recovery import execute_recoverable_v2_work
        self.start(); work2 = self.root/'work2'
        execute_recoverable_v2_work(self.text, 'feature', 'Second', work2, self.config)
        recorded = {w: json.loads(WorkWorkspace.open(w/'recovery').read_artifact('baseline'))['snapshot']['identity']['git_dir']
                    for w in (self.work, work2)}
        self.assertNotEqual(*recorded.values())  # Git named them workspace and workspace1.
        suspend_v2_work(work2); suspend_v2_work(self.work)  # Both admin names are now free.
        for w in (work2, self.work):  # work2 first: Git would name it after the free basename.
            self.assertEqual(resume_v2_work(w, self.text, self.config)['terminal'], 'READY_FOR_MANUAL_QA')
            self.assertEqual(self.git('rev-parse', '--absolute-git-dir', cwd=w/'workspace'), recorded[w])
            self.assertNotIn('isolation_blocked', recovery_status(w))
        self.assertEqual(self.git('status', '--porcelain', cwd=work2/'workspace'), self.git('status', '--porcelain', cwd=self.delivery))


class RestoreLifecycleTests(RestoreBase):
    def ready(self):
        execution = self.start()
        create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.root/'qa')
        attach_qa_candidate(self.work, self.root/'qa')

    # 19, 30
    def test_qa_candidate_history_survive_and_restored_work_can_be_accepted(self):
        self.ready()
        log = self.root/'console.log'; log.write_bytes(b'TypeError\n')
        qa_fix_v2_work(self.work, None, self.text, self.config, findings=[{'description': 'Rename fails', 'attachments': [str(log)]}])
        before = recovery_status(self.work); history = qa_history(self.work)
        suspend_v2_work(self.work); self.advance_source()
        status = resume_v2_work(self.work, self.text, self.config)
        for key in ('terminal', 'qa_candidate', 'current_candidate_sequence', 'qa_fix_rounds', 'qa_round_open', 'qa_fixed_findings'):
            self.assertEqual(status[key], before[key], key)
        self.assertEqual(qa_history(self.work), history)
        self.assertEqual(sorted(p.name for p in (self.work/'qa').iterdir()), ['candidate-2', 'evidence'])  # No new candidate.
        decision = accept_v2_candidate(self.work)
        self.assertEqual(decision.terminal, 'ACCEPTED')
        self.assertEqual(self.git('rev-parse', decision.committed_head+'^'), self.base)
        self.assertEqual(recovery_status(self.work)['accepted_commit_sha'], decision.committed_head)
        self.assertEqual([f['status'] for r in qa_history(self.work)['rounds'] for f in r['findings']], ['verified'])

    # 28, 29
    def test_two_suspend_resume_cycles_keep_immutable_shelves(self):
        self.assertEqual(self.start().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.dirty_staged(); s1 = self.picture(); events = self.events()
        suspend_v2_work(self.work)
        shelf1 = {p: p.read_bytes() for p in self.shelf().rglob('*') if p.is_file()}
        self.assertEqual(resume_v2_work(self.work, self.text, self.config)['terminal'], 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.picture(), s1)
        (self.delivery/'notes/new.bin').write_bytes(b'\x01second state\xff'); (self.delivery/'script.sh').chmod(0o644)
        s2 = self.picture()
        status = suspend_v2_work(self.work, 'again')
        self.assertEqual((status['shelf_id'], status['terminal']), ('shelf-2', 'SUSPENDED'))
        self.assertEqual({p: p.read_bytes() for p in self.shelf().rglob('*') if p.is_file()}, shelf1)  # Immutable.
        verify_shelf(self.shelf('shelf-2'))
        self.assertEqual(resume_v2_work(self.work, self.text, self.config)['terminal'], 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.picture(), s2)
        self.assertEqual([o['restored_shelf'] for o in self.ops() if o['phase'] == 'delivery_done' and 'restored_shelf' in o],
                         ['shelf-1', 'shelf-2'])
        self.assertEqual((recovery_status(self.work)['work_id'], self.events()), ('work', events))


class RestoreHumanWaitTests(RestoreBase):
    # 17
    def test_waiting_for_human_restores_the_same_request_without_answering(self):
        states = self.data['flows']['feature']['states']
        states['SPECIFY'].update(rung_budget_scope='visit', budget={'visits': 3}, on={'ready': 'IMPLEMENT', 'needs_clarification': 'DECISION'})
        states['DECISION'] = dict(wait_for='human_input', resume_to='SPECIFY', budget={'visits': 2, 'on_exceeded': 'NEEDS_HUMAN'})
        self.text = json.dumps(self.data)
        class Stage:
            def run(self, invocation):
                return StageResult('needs_clarification', handoff=StageHandoff('Need a decision.', blockers=('Keep the old API?',)))
        with patch('switchyard.recovery.ProviderStageExecutorResolver', return_value=lambda binding: Stage()):
            self.start()
        waiting = recovery_status(self.work)
        self.assertTrue(waiting['human_input_pending'])
        (self.delivery/'draft.md').write_text('partial notes\n'); before = self.picture(); events = self.events()
        suspend_v2_work(self.work)
        status = resume_v2_work(self.work, self.text, self.config)
        for key in ('human_input_pending', 'human_input_request_id', 'question', 'phase'):
            self.assertEqual(status[key], waiting[key], key)
        self.assertEqual((self.picture(), self.events()), (before, events))
        with self.assertRaises(RecoveryBlocked) as ctx:  # Still requires a human answer; never answered automatically.
            resume_v2_work(self.work, self.text, self.config)
        self.assertTrue(ctx.exception.reason.startswith('human_input_required'))


if __name__ == '__main__':
    unittest.main()
