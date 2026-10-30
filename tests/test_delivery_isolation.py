"""DELIVERY-ISOLATION (#3): one delivery branch + one Git worktree per work. Real Git, real
provider processes (offline fixture CLIs), real command verifier; no live providers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import ExecutionError
from switchyard import recovery
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (execute_recoverable_v2_work, resume_v2_work, rollback_v2_work, recovery_status,
                                 attach_qa_candidate, qa_fix_v2_work, accept_v2_candidate, RecoveryBlocked)
from switchyard.work_contract import WorkWorkspace

ROOT = Path(__file__).resolve().parents[1]
REQUEST = 'Change source.py'


class Crash(BaseException):
    """Process death: bypasses every `except Exception` cleanup path."""


def crash_once(original):
    state = {'done': False}
    def wrapper(*args, **kwargs):
        if not state['done']:
            state['done'] = True
            raise Crash()
        return original(*args, **kwargs)
    return wrapper


class IsolationBase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.target = self.root/'target'; self.target.mkdir()
        self.work = self.root/'work'; self.delivery = self.work/'workspace'
        (self.target/'source.py').write_text('before')
        (self.target/'.gitignore').write_text('.switchyard/*\n!.switchyard/config.yaml\n__pycache__/\n*.log\n')
        (self.target/'.switchyard').mkdir()
        self.verify_cwd = self.root/'verify-cwd'
        check = ("import os, pathlib; pathlib.Path(%r).write_text(os.getcwd()); "
                 "assert pathlib.Path('source.py').read_text()=='changed'" % str(self.verify_cwd))
        self.data = {'agents': {
            'spec': dict(provider='codex', model='offline', workspace='read', shell=True, timeout=5),
            'code': dict(provider='claude', model='sonnet', workspace='write', shell=True, timeout=5),
            'tests': dict(provider='command', command=[sys.executable, '-c', check], capture='verifier_evidence', timeout=5)},
            'flows': {'feature': {'start': 'SPECIFY', 'limits': {'run_invocations': 12}, 'states': {
                'NEEDS_HUMAN': dict(terminal=True), 'READY_FOR_MANUAL_QA': dict(terminal=True, resume_to='IMPLEMENT'),
                'SPECIFY': dict(agent='spec', consumes=['request'], produces={'microspec': dict(format='yaml', schema='microspec')}, on={'ready': 'IMPLEMENT'}),
                'IMPLEMENT': dict(run=[dict(agent='code', budget={'invocations': 3})], budget={'visits': 2}, consumes=['microspec'],
                                  produces={'implementation_handoff': dict(format='markdown')}, on={'ready': 'VERIFY'}),
                'VERIFY': dict(agent='tests', rung_budget_scope='visit', budget={'visits': 2}, consumes=['implementation_handoff'], produces={'verifier_evidence': dict(format='markdown')},
                               on={'pass': 'READY_FOR_MANUAL_QA', 'fail': 'NEEDS_HUMAN'})}}}}
        self.text = json.dumps(self.data); (self.target/'.switchyard/config.yaml').write_text(self.text)
        for args in [('init', '-b', 'main'), ('config', 'user.name', 'Isolation Tests'), ('config', 'user.email', 'iso@example.invalid'),
                     ('config', 'commit.gpgsign', 'false'), ('add', '--', '.gitignore', '.switchyard/config.yaml', 'source.py'),
                     ('commit', '-m', 'A')]:
            self.git(*args)
        self.base = self.git('rev-parse', 'HEAD')
        self.plan = self.root/'plan'; self.plan.write_text('{"artifacts":{}}')
        self.log = self.root/'calls'; self.fail_once = self.root/'fail-once'
        mutation = '''if state=='IMPLEMENT':
    flag=Path(os.environ['FAIL_ONCE'])
    if flag.exists():
        flag.unlink(); sys.exit(7)
    Path('source.py').write_text('changed')
'''
        script = (ROOT/'tests/fixtures/provider_cli.py').read_text().replace('outcome=plan.get', mutation+'outcome=plan.get')
        for name in ('claude', 'codex'):
            exe = self.root/name; exe.write_text('#!'+sys.executable+'\n'+script); exe.chmod(0o755)
        self.config = self.provider(self.target)

    def provider(self, directory):
        return ProviderRuntimeConfig(str(directory), str(self.root/'claude'), str(self.root/'codex'),
            environment={**os.environ, 'FAKE_PLAN': str(self.plan), 'FAKE_LOG': str(self.log),
                         'FAKE_CONTRACT_AWARE': '1', 'FAIL_ONCE': str(self.fail_once)})

    def git(self, *args, cwd=None):
        env = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')}, 'GIT_OPTIONAL_LOCKS': '0'}
        return subprocess.check_output(['git', *args], cwd=cwd or self.target, env=env, stderr=subprocess.DEVNULL).decode().strip()

    def start(self):
        return execute_recoverable_v2_work(self.text, 'feature', REQUEST, self.work, self.config)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def branches(self):
        return self.git('for-each-ref', '--format=%(refname)', 'refs/heads/switchyard/').split()

    def worktrees(self):
        return [l[9:] for l in self.git('worktree', 'list', '--porcelain').splitlines() if l.startswith('worktree ')]

    def source_state(self):
        files = {str(p.relative_to(self.target)): p.read_bytes() for p in self.target.rglob('*')
                 if p.is_file() and '.git' not in p.relative_to(self.target).parts}
        return dict(head=self.git('rev-parse', 'HEAD'), branch=self.git('symbolic-ref', 'HEAD'),
                    index=(self.target/'.git/index').read_bytes(), files=files,
                    status=self.git('status', '--porcelain', '--untracked-files=all'))


class IsolatedDeliveryTests(IsolationBase):
    # 1, 2, 3, 4, 5, 6, 7, 19
    def test_clean_start_runs_every_stage_in_its_own_worktree_at_the_exact_base(self):
        before = self.source_state()
        execution = self.start()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        status = recovery_status(self.work)
        self.assertEqual((status['isolated'], status['base_sha'], status['delivery_branch'], status['delivery_worktree'],
                          status['source_worktree'], status['target']),
                         (True, self.base, 'refs/heads/switchyard/work', str(self.delivery), str(self.target), str(self.delivery)))
        self.assertEqual(len(self.base), 40)
        self.assertNotIn('isolation_blocked', status)
        # Branch at the exact base, checked out only in the dedicated worktree.
        self.assertEqual(self.branches(), ['refs/heads/switchyard/work'])
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), self.base)
        self.assertEqual(self.git('symbolic-ref', 'HEAD', cwd=self.delivery), 'refs/heads/switchyard/work')
        self.assertEqual(sorted(self.worktrees()), sorted([str(self.target), str(self.delivery)]))
        # Model stages and verifier ran in the delivery worktree; edits landed there only.
        self.assertEqual({c['cwd'] for c in self.calls()}, {str(self.delivery)})
        self.assertIn('--cd', self.calls()[0]['argv']); self.assertIn(str(self.delivery), self.calls()[0]['argv'])
        self.assertEqual(self.verify_cwd.read_text(), str(self.delivery))
        self.assertEqual((self.delivery/'source.py').read_text(), 'changed')
        self.assertEqual(self.source_state(), before)  # HEAD, branch, index bytes, file bytes, status.
        # Candidate: delivery worktree relative to base_sha.
        create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.root/'qa')
        candidate = json.loads(WorkWorkspace.open(self.root/'qa').read_artifact('qa_candidate'))
        self.assertEqual((candidate['target'], candidate['branch'], candidate['base_head'], sorted(candidate['files'])),
                         (str(self.delivery), 'refs/heads/switchyard/work', self.base, ['source.py']))
        attach_qa_candidate(self.work, self.root/'qa')
        self.assertEqual(self.source_state(), before)

    # 8, 9, 10
    def test_dirty_source_refuses_before_any_provisioning(self):
        cases = [('modified tracked', lambda: (self.target/'source.py').write_text('local edit'), 'source.py'),
                 ('staged', lambda: ((self.target/'new.py').write_text('x'), self.git('add', 'new.py')), 'new.py'),
                 ('untracked', lambda: (self.target/'notes.txt').write_text('mine'), 'notes.txt')]
        for label, dirty, path in cases:
            with self.subTest(label):
                dirty()
                before = self.source_state()
                with self.assertRaises(RecoveryBlocked) as ctx:
                    self.start()
                self.assertEqual(ctx.exception.terminal, 'DELIVERY_BLOCKED')
                self.assertTrue(ctx.exception.reason.startswith('dirty_source_worktree'))
                self.assertIn('Commit, stash, or remove local changes first.', str(ctx.exception))
                self.assertIn(path, ctx.exception.paths)
                self.assertEqual(self.source_state(), before)  # Nothing stashed, reset, copied or deleted.
                self.assertEqual((self.branches(), self.worktrees()), ([], [str(self.target)]))
                self.assertFalse((self.work/'recovery').exists()); self.assertEqual(self.calls(), [])
                self.git('reset', '-q'); self.git('checkout', '-q', '--', 'source.py')
                for leftover in ('new.py', 'notes.txt'):
                    (self.target/leftover).unlink(missing_ok=True)

    def test_untracked_policy_is_gitignore_and_only_config_is_exempt(self):
        (self.target/'build.log').write_text('ignored by .gitignore')
        (self.target/'.switchyard/cache').write_text('ignored by .gitignore')
        (self.target/'.switchyard/config.yaml').write_text(self.text+'\n')  # Frozen as text; never copied.
        self.assertEqual(self.start().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertFalse((self.delivery/'build.log').exists())
        self.assertEqual((self.delivery/'.switchyard/config.yaml').read_text(), self.text)

    def test_work_root_inside_source_and_unsafe_work_id_are_refused(self):
        for work in (self.target/'.switchyard/work', self.root/'bad name'):
            with self.subTest(str(work)), self.assertRaises(RecoveryBlocked) as ctx:
                execute_recoverable_v2_work(self.text, 'feature', REQUEST, work, self.config)
            self.assertIn(ctx.exception.reason, ('work_root_inside_source_worktree', 'work_id_not_a_safe_branch_name'))
        self.assertEqual(self.branches(), [])

    # 11, 12
    def test_source_advancing_does_not_move_the_delivery_and_resume_uses_recorded_workspace(self):
        self.fail_once.write_text('1')
        with self.assertRaises(ExecutionError):
            self.start()
        self.assertEqual(recovery_status(self.work)['terminal'], 'INTERRUPTED')
        (self.target/'source.py').write_text('B'); self.git('commit', '-qam', 'B'); b = self.git('rev-parse', 'HEAD')
        (self.target/'unrelated.py').write_text('user work in progress')  # Dirty source no longer matters.
        before = self.source_state()
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.provider(self.root))
        self.assertEqual(ctx.exception.reason, 'provider_workspace_mismatch')
        execution = resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        status = recovery_status(self.work)
        self.assertEqual((status['base_sha'], status['delivery_worktree']), (self.base, str(self.delivery)))
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.delivery), self.base)  # Not rebased onto B.
        self.assertEqual((self.delivery/'source.py').read_text(), 'changed')
        self.assertEqual({c['cwd'] for c in self.calls()}, {str(self.delivery)})
        self.assertEqual(self.source_state(), before); self.assertEqual(self.git('rev-parse', 'main'), b)
        create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.root/'qa')
        candidate = json.loads(WorkWorkspace.open(self.root/'qa').read_artifact('qa_candidate'))
        self.assertEqual(candidate['base_head'], self.base)  # A -> delivery changes, never B -> ...

    # 13
    def test_crash_after_branch_creation_retries_onto_the_same_branch(self):
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.start()
        self.assertEqual(self.branches(), ['refs/heads/switchyard/work']); self.assertFalse(self.delivery.exists())
        with self.assertRaises(RecoveryBlocked) as ctx:
            recovery_status(self.work)
        self.assertTrue(ctx.exception.reason.startswith('delivery_provisioning_incomplete'))
        self.assertEqual(self.start().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.branches(), ['refs/heads/switchyard/work'])
        self.assertEqual(sorted(self.worktrees()), sorted([str(self.target), str(self.delivery)]))
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.start()  # Once running, a start is never repeated.
        self.assertEqual(ctx.exception.reason, 'workspace_not_fresh')

    # 14
    def test_crash_after_worktree_creation_or_baseline_retries_onto_the_same_worktree(self):
        for name, crash in (('_check_isolation', crash_once(recovery._check_isolation)),
                            ('_operation', None)):
            with self.subTest(name):
                shutil.rmtree(self.work, ignore_errors=True); self.git('worktree', 'prune')
                for ref in self.branches():
                    self.git('update-ref', '-d', ref)
                if crash is None:  # Baseline recorded, crash before `running`.
                    original = recovery._operation
                    state = {'done': False}
                    def crash(store, phase, **fields):
                        if phase == 'running' and not state['done']:
                            state['done'] = True; raise Crash()
                        return original(store, phase, **fields)
                with patch('switchyard.recovery.'+name, crash), self.assertRaises(Crash):
                    self.start()
                self.assertTrue((self.delivery/'.git').is_file())
                self.assertEqual(self.start().result.terminal_state, 'READY_FOR_MANUAL_QA')
                self.assertEqual(self.branches(), ['refs/heads/switchyard/work'])
                self.assertEqual(sorted(self.worktrees()), sorted([str(self.target), str(self.delivery)]))

    # 15
    def test_incompatible_existing_branch_is_refused_never_reset(self):
        self.git('branch', 'switchyard/work')  # Pre-existing, even at the same base: not provably ours.
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.start()
        self.assertEqual(ctx.exception.reason, 'delivery_branch_exists')
        self.assertFalse((self.work/'recovery').exists())
        self.git('branch', '-D', 'switchyard/work')
        # Retry case: our intent is durable, but the branch was moved to another lineage meanwhile.
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.start()
        self.git('checkout', '-q', '-b', 'other'); (self.target/'x.py').write_text('x'); self.git('add', 'x.py')
        self.git('commit', '-qm', 'other'); other = self.git('rev-parse', 'HEAD'); self.git('checkout', '-q', 'main')
        self.git('update-ref', 'refs/heads/switchyard/work', other)
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.start()
        self.assertEqual(ctx.exception.reason, 'delivery_branch_conflict')
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), other)  # Never force-reset.
        self.assertFalse(self.delivery.exists()); self.assertEqual(self.calls(), [])

    # 16
    def test_ambiguous_existing_worktree_path_is_refused_never_deleted(self):
        self.delivery.mkdir(parents=True); (self.delivery/'keep.txt').write_text('user data')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.start()
        self.assertEqual(ctx.exception.reason, 'delivery_worktree_path_exists')
        self.assertEqual(self.branches(), [])
        # Retry case: the path appears after the durable intent and branch.
        shutil.rmtree(self.work)
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.start()
        self.delivery.mkdir(); (self.delivery/'keep.txt').write_text('user data')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.start()
        self.assertEqual(ctx.exception.reason, 'delivery_worktree_not_owned')
        self.assertEqual((self.delivery/'keep.txt').read_text(), 'user data'); self.assertEqual(self.calls(), [])

    # 17
    def test_missing_or_repointed_worktree_fails_explicitly(self):
        self.fail_once.write_text('1')
        with self.assertRaises(ExecutionError):
            self.start()
        calls = len(self.calls())
        self.git('checkout', '-q', '-b', 'elsewhere', cwd=self.delivery)
        self.assertEqual(recovery_status(self.work)['isolation_blocked'], 'delivery_branch_mismatch')
        with self.assertRaises(RecoveryBlocked) as ctx:
            resume_v2_work(self.work, self.text, self.config)
        self.assertEqual(ctx.exception.reason, 'delivery_branch_mismatch')
        self.git('checkout', '-q', 'switchyard/work', cwd=self.delivery)
        shutil.rmtree(self.delivery)
        self.assertEqual(recovery_status(self.work)['isolation_blocked'], 'delivery_worktree_missing')
        for operation in (lambda: resume_v2_work(self.work, self.text, self.config), lambda: rollback_v2_work(self.work)):
            with self.assertRaises(RecoveryBlocked) as ctx:
                operation()
            self.assertEqual(ctx.exception.reason, 'delivery_worktree_missing')
        self.assertFalse(self.delivery.exists())  # Never silently recreated.
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), self.base)

    # 18
    def test_qa_corrective_cycle_and_accept_stay_in_the_delivery_worktree(self):
        before = self.source_state()
        execution = self.start()
        create_qa_candidate(self.delivery, execution.workspace, execution.result, 'feature', self.root/'qa')
        attach_qa_candidate(self.work, self.root/'qa')
        calls = len(self.calls())
        qa_fix_v2_work(self.work, 'Still wrong from the menu.', self.text, self.config)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['current_candidate_sequence']), ('READY_FOR_MANUAL_QA', 2))
        self.assertEqual({c['cwd'] for c in self.calls()[calls:]}, {str(self.delivery)})
        self.assertTrue(status['qa_candidate']['workspace'].startswith(str(self.work/'qa')))
        self.assertEqual(json.loads(WorkWorkspace.open(status['qa_candidate']['workspace']).read_artifact('qa_candidate'))['target'],
                         str(self.delivery))
        self.assertEqual(accept_v2_candidate(self.work).terminal, 'ACCEPTED')
        # Acceptance commits on the delivery branch only; nothing is merged into the source.
        self.assertEqual(self.git('rev-parse', 'switchyard/work^'), self.base)
        self.assertEqual(self.git('show', 'switchyard/work:source.py'), 'changed')
        self.assertEqual(self.source_state(), before)

    def test_source_checkout_candidate_cannot_be_attached(self):
        execution = self.start()
        (self.target/'source.py').write_text('user edit')  # Capturable, but not this delivery's work.
        create_qa_candidate(self.target, execution.workspace, execution.result, 'feature', self.root/'wrong')
        with self.assertRaises(RecoveryBlocked) as ctx:
            attach_qa_candidate(self.work, self.root/'wrong')
        self.assertEqual(ctx.exception.reason, 'qa_candidate_not_from_delivery_worktree')


class LegacyInPlaceTests(IsolationBase):
    # 20
    def test_legacy_in_place_work_is_still_read_resumed_and_rolled_back(self):
        self.fail_once.write_text('1')
        with self.assertRaises(ExecutionError):
            execute_recoverable_v2_work(self.text, 'feature', REQUEST, self.work, self.config, isolation=False)
        self.assertNotIn('isolation', json.loads(WorkWorkspace.open(self.work/'recovery').read_artifact('baseline')))
        status = recovery_status(self.work)
        self.assertEqual((status['isolated'], status['target'], status['terminal']), (False, str(self.target), 'INTERRUPTED'))
        self.assertNotIn('delivery_worktree', status)
        self.assertEqual(resume_v2_work(self.work, self.text, self.config).result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual((self.target/'source.py').read_text(), 'changed')  # Legacy: the caller's checkout.
        self.assertEqual((self.branches(), self.worktrees()), ([], [str(self.target)]))
        self.assertEqual(rollback_v2_work(self.work)['terminal'], 'ROLLED_BACK')
        self.assertEqual((self.target/'source.py').read_text(), 'before')


if __name__ == '__main__':
    unittest.main()
