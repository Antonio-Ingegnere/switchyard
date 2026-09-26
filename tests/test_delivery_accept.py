import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from examples.delivery.__main__ import main
from examples.delivery.acceptance import AcceptanceApproval, GitAcceptance
from examples.delivery.presets import build_accept
from runtime import RunContext, WorkUnit, run
from runtime.recording import JsonlRecorder
from switchyard.observability import read_events, summarize_runs


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.git_executable = shutil.which('git')
        if self.git_executable is None:
            self.skipTest('Git executable unavailable')
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Offline Test')
        self.git('config', 'user.email', 'offline@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        (self.root / 'styles.css').write_text('original\n')
        self.git('add', 'styles.css')
        self.git('commit', '-m', 'base')
        self.original = self.git('rev-parse', 'HEAD').strip()
        (self.root / 'styles.css').write_text('accepted change\n')
        self.git('add', 'styles.css')
        self.session = GitAcceptance(str(self.root), self.git_executable)
        snapshot = self.session.inspect()
        self.approval = AcceptanceApproval(snapshot.head, snapshot.patch_sha256, 'Accepted isolated change', True)

    def git(self, *args):
        return subprocess.run([self.git_executable, *args], cwd=self.root, capture_output=True,
                              check=True, timeout=10).stdout.decode()

    def execute(self, recorder=None):
        loaded = build_accept(workspace=str(self.root), git_executable=self.git_executable, approval=self.approval)
        result = run(WorkUnit('accept after human QA'), loaded.flow, loaded.executors,
                     recorder=recorder, context=RunContext('change-123', 'prior-qa-ready-run'))
        return result

    def test_clean_approved_change_commits_exact_tree_and_records(self):
        expected_tree = self.git('write-tree').strip()
        path = self.base / 'events.jsonl'
        result = self.execute(JsonlRecorder(path))
        self.assertEqual(result.terminal_state, 'ACCEPTED')
        self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'],
                         ['CHECK_WORKTREE', 'CHECK_SCOPE', 'COMMIT'])
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}').strip(), expected_tree)
        self.assertEqual(self.git('rev-parse', 'HEAD^').strip(), self.original)
        self.assertEqual(self.git('log', '-1', '--format=%s').strip(), 'Accepted isolated change')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(tuple(read_events(path)), result.events)
        summary, = summarize_runs(read_events(path))
        self.assertEqual(summary.parent_run_id, 'prior-qa-ready-run')
        self.assertEqual(summary.correlation_id, 'change-123')
        self.assertIsNotNone(summary.run_duration_ns)

    def test_mixed_same_file_patch_is_blocked_without_commit(self):
        (self.root / 'styles.css').write_text('accepted change\nunrelated styling work\n')
        self.git('add', 'styles.css')
        result = self.execute()
        self.assertEqual(result.terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)
        self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'], ['CHECK_WORKTREE', 'CHECK_SCOPE'])
        self.assertIn('unrelated styling', (self.root / 'styles.css').read_text())

    def test_unrelated_staged_file_is_blocked(self):
        (self.root / 'unrelated.txt').write_text('other work')
        self.git('add', 'unrelated.txt')
        result = self.execute()
        self.assertEqual(result.terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)

    def test_untracked_and_unstaged_changes_block_worktree(self):
        (self.root / 'untracked.txt').write_text('other work')
        self.assertEqual(self.execute().terminal_state, 'COMMIT_BLOCKED')
        (self.root / 'untracked.txt').unlink()
        (self.root / 'styles.css').write_text('unstaged other work')
        self.assertEqual(self.execute().terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)

    def test_stale_head_and_missing_human_approval(self):
        self.git('commit', '--allow-empty', '--only', '-m', 'external history change')
        head = self.git('rev-parse', 'HEAD').strip()
        self.assertNotEqual(head, self.original)
        self.assertEqual(self.execute().terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), head)
        with self.assertRaises(ValueError):
            AcceptanceApproval(self.approval.base_commit, self.approval.patch_sha256, 'message', False)

    def test_scope_rechecked_immediately_before_commit(self):
        class ChangeAfterScope:
            def record(inner, event):
                if event.kind == 'state_exited' and event.state == 'CHECK_SCOPE':
                    (self.root / 'styles.css').write_text('new mixed scope')
                    self.git('add', 'styles.css')
        self.assertEqual(self.execute(ChangeAfterScope()).terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)

    def test_changed_index_during_commit_cannot_enter_approved_tree(self):
        expected_tree = self.git('write-tree').strip()
        original_git = GitAcceptance.git
        def git(session, *args, **kwargs):
            if args[0] == 'commit-tree':
                (self.root / 'unrelated.txt').write_text('concurrent unrelated change')
                self.git('add', 'unrelated.txt')
            self.assertNotEqual(args[0], 'push')
            return original_git(session, *args, **kwargs)
        with patch.object(GitAcceptance, 'git', git):
            result = self.execute()
        self.assertEqual(result.terminal_state, 'ACCEPTED')
        self.assertEqual(self.git('rev-parse', 'HEAD^{tree}').strip(), expected_tree)
        self.assertEqual(self.git('ls-tree', '--name-only', 'HEAD').splitlines(), ['styles.css'])
        self.assertIn('unrelated.txt', self.git('diff', '--cached', '--name-only'))

    def test_compare_and_swap_refuses_concurrent_history_change(self):
        original_git = GitAcceptance.git
        competing = []
        def git(session, *args, **kwargs):
            if args[0] == 'update-ref':
                self.git('commit', '--allow-empty', '--only', '-m', 'concurrent commit')
                competing.append(self.git('rev-parse', 'HEAD').strip())
            return original_git(session, *args, **kwargs)
        with patch.object(GitAcceptance, 'git', git):
            result = self.execute()
        self.assertEqual(result.terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), competing[0])
        self.assertEqual(self.git('log', '-1', '--format=%s').strip(), 'concurrent commit')

    def test_execution_artifacts_and_merge_state_block(self):
        artifacts = self.root / '.switchyard'
        artifacts.mkdir()
        (artifacts / 'analysis.md').write_text('execution context')
        self.git('add', '.switchyard')
        self.assertEqual(self.execute().terminal_state, 'COMMIT_BLOCKED')
        # A separate invocation must also block an in-progress merge.
        (self.root / '.git' / 'MERGE_HEAD').write_text(self.original + '\n')
        self.assertEqual(self.execute().terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)

    def test_application_inspect_then_explicit_accept_flags(self):
        common = ['--workspace', str(self.root), '--git-executable', self.git_executable]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['inspect', *common]), 0)
        inspection = json.loads(out.getvalue())
        self.assertEqual(inspection['base_commit'], self.original)
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = main(['accept', *common, '--task', 'scope reviewed', '--correlation-id', 'c',
                           '--parent-run-id', 'prior', '--approved-head', inspection['base_commit'],
                           '--approved-patch-sha256', inspection['patch_sha256'],
                           '--message', 'human accepted', '--manual-qa-passed',
                           '--events-file', str(self.base / 'events.jsonl')])
        self.assertEqual(status, 0)
        self.assertIn('Terminal: ACCEPTED', out.getvalue())


    def test_hidden_worktree_edits_and_active_sequencer_block(self):
        for flag, undo in (('--assume-unchanged', '--no-assume-unchanged'),
                           ('--skip-worktree', '--no-skip-worktree')):
            self.git('update-index', flag, 'styles.css')
            self.assertEqual(self.execute().terminal_state, 'COMMIT_BLOCKED')
            self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)
            self.git('update-index', undo, 'styles.css')
        (self.root / '.git' / 'sequencer').mkdir()
        self.assertEqual(self.execute().terminal_state, 'COMMIT_BLOCKED')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.original)
