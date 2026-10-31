"""INTEGRATION (#9) slice 3: integrate into a target branch checked out in exactly one clean worktree."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from switchyard import integration
from switchyard.recovery import accept_v2_candidate, recovery_status, _Repo

from test_delivery_isolation import Crash
from test_integrate import IntegrateBase


class CheckoutBase(IntegrateBase):
    def clean_at(self, sha, root=None):
        root = root or self.target
        self.assertEqual((self.git('rev-parse', 'HEAD', cwd=root), self.git('symbolic-ref', 'HEAD', cwd=root)), (sha, 'refs/heads/main'))
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all', cwd=root), '')
        subprocess_ok = lambda *a: __import__('subprocess').run(['git', *a], cwd=root, capture_output=True).returncode == 0
        self.assertTrue(subprocess_ok('diff-index', '--cached', '--quiet', sha, '--'))  # Index == N.
        self.assertTrue(subprocess_ok('diff', '--quiet', sha, '--'))                    # Worktree == N.

    def ready_checked_out(self):
        """W1 accepted while the source checkout keeps `main` checked out and clean."""
        self.w1_ready(); source = self.source_state()
        c = accept_v2_candidate(self.work).committed_head
        self.assertEqual(self.source_state(), source)  # Accept never touches the source checkout.
        return c


class CheckedOutIntegrationTests(CheckoutBase):
    # 1-6, 18, 19, 22-25
    def test_ready_fast_forwards_the_clean_checked_out_source_checkout(self):
        c = self.ready_checked_out(); a = self.assess(); calls = self.model_calls(); commits = self.commits()
        r = self.integrate()
        self.assertEqual((r['mode'], r['strategy'], r['target_worktree'], r['target_worktree_git_dir'], r['target_worktree_updated'],
                          r['integrated_commit_sha'], r['target_before_sha']),
                         ('checked_out_worktree', 'fast_forward', str(self.target), str((self.target/'.git').resolve()), True, c, self.base))
        self.assertEqual((self.git('rev-parse', 'main'), self.commits()), (c, commits))  # No extra commit.
        self.clean_at(c)
        self.assertEqual(((self.target/'source.py').read_text(), (self.target/'logo.png').read_bytes()), ('changed', self.files(self.delivery) and (self.delivery/'logo.png').read_bytes()))
        self.assertFalse((self.target/'obsolete.py').exists()); self.assertTrue(os.access(self.target/'tool.sh', os.X_OK))
        self.assertEqual((self.git('rev-parse', 'switchyard/work'), self.model_calls()), (c, calls))
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['integration']['state'], status['integration']['target_worktree_updated'],
                          status['integration']['target_worktree']), ('ACCEPTED', 'INTEGRATED', True, str(self.target)))
        stamp = (self.target/'source.py').stat().st_mtime_ns
        self.assertEqual(self.integrate(), r)  # Idempotent: no second install.
        self.assertEqual(((self.target/'source.py').stat().st_mtime_ns, self.history()), (stamp, ['intent', 'integrated']))

    # 7, 8
    def test_clean_drift_installs_the_deterministic_merge_commit(self):
        c = self.ready_checked_out(); t = self.advance(write={'keep.txt': 'upstream\n'})
        a = self.assess(); r = self.integrate(); m = r['integrated_commit_sha']
        self.assertEqual((self.parents(m), self.git('rev-parse', m+'^{tree}')), ([t, c], a['result_tree']))
        self.assertEqual(integration._merge_commit(_Repo(self.target/'.git'), r, write=False), m)  # The plan's M, not a live merge.
        self.clean_at(m)
        self.assertEqual(((self.target/'keep.txt').read_text(), (self.target/'source.py').read_text()), ('upstream\n', 'changed'))

    # 20, 21
    def test_unrelated_dirty_worktrees_are_irrelevant(self):
        c = self.ready_checked_out(); other = self.root/'side'
        self.git('worktree', 'add', '-q', '--detach', str(other), self.base)
        (other/'source.py').write_text('side edit'); (other/'side.txt').write_text('untracked')
        self.assess(); self.integrate()
        self.clean_at(c)
        self.assertEqual(((other/'source.py').read_text(), (other/'side.txt').read_text()), ('side edit', 'untracked'))


class DirtyCheckoutTests(CheckoutBase):
    def refused(self, reason):
        source = self.source_state(); before = self.git('rev-parse', 'main')
        e = self.blocked(self.integrate)
        self.assertTrue(e.reason.startswith(reason), e.reason)
        self.assertEqual((self.source_state(), self.git('rev-parse', 'main'), self.history()), (source, before, []))
        return e

    # 9-13
    def test_any_local_state_in_the_target_checkout_blocks_before_anything_changes(self):
        self.ready_checked_out(); self.assess()
        (self.target/'keep.txt').write_text('local edit\n')
        self.assertEqual(self.refused('target_worktree_dirty').paths, ('keep.txt',))
        self.git('checkout', '--', 'keep.txt')
        (self.target/'staged.txt').write_text('s'); self.git('add', 'staged.txt')
        self.refused('target_worktree_dirty'); self.git('rm', '-q', '--cached', 'staged.txt'); (self.target/'staged.txt').unlink()
        (self.target/'stray.txt').write_text('untracked')
        self.assertEqual(self.refused('target_worktree_dirty').paths, ('stray.txt',)); (self.target/'stray.txt').unlink()
        (self.target/'.git/info/exclude').write_text('notes/\n')
        (self.target/'notes').mkdir(); (self.target/'notes/new.bin').write_bytes(b'ignored but in the way')
        e = self.refused('target_worktree_dirty: ignored files where the integration adds paths')
        self.assertEqual(e.paths, ('notes/new.bin',)); self.assertEqual((self.target/'notes/new.bin').read_bytes(), b'ignored but in the way')
        (self.target/'notes/new.bin').unlink()
        (self.target/'.git/MERGE_HEAD').write_text(self.base+'\n')
        self.refused('target_worktree_dirty: Git operation in progress'); (self.target/'.git/MERGE_HEAD').unlink()
        (self.target/'.git/rebase-merge').mkdir()
        self.refused('target_worktree_dirty: Git operation in progress'); (self.target/'.git/rebase-merge').rmdir()
        (self.target/'.git/index.lock').write_text('')
        self.refused('target_worktree_locked'); (self.target/'.git/index.lock').unlink()
        self.assertEqual(self.integrate()['mode'], 'checked_out_worktree')

    # 14
    def test_a_target_moving_before_ownership_blocks_with_no_file_changes(self):
        self.ready_checked_out(); self.assess()
        t2 = self.advance(write={'keep.txt': 'T2\n'}, message='T2')
        e = self.refused('target_moved')
        self.assertEqual(e.integration['actual_target_sha'], t2)


class CheckoutCrashTests(CheckoutBase):
    def drifted(self):
        c = self.ready_checked_out(); t = self.advance(write={'keep.txt': 'T\n'}); self.assess()
        return c, t

    # 15
    def test_crash_after_the_ref_moved_before_the_checkout_was_updated(self):
        c, t = self.drifted()
        with patch('switchyard.integration._install', side_effect=Crash()), self.assertRaises(Crash):
            self.integrate()
        m = self.git('rev-parse', 'main')
        self.assertEqual(((self.target/'keep.txt').read_text(), self.parents(m)), ('T\n', [t, c]))  # HEAD N, files still T.
        self.assertEqual(recovery_status(self.work)['integration']['state'], 'INTEGRATION_PENDING')
        r = self.integrate()
        self.assertEqual((r['integrated_commit_sha'], self.history()), (m, ['intent', 'integrated']))
        self.clean_at(m)

    def interrupted_install(self, lock):
        c, t = self.drifted()
        def partial(wt, t_, n, state):
            (wt.workspace/'source.py').write_text('changed')  # Part of N written ...
            (wt.workspace/'notes').mkdir(); (wt.workspace/'notes/new.bin').write_bytes((self.delivery/'notes/new.bin').read_bytes())
            if lock:
                Path(str(wt.index_path())+'.lock').write_bytes(b'held by someone')  # ... index not yet committed.
            raise Crash()
        with patch('switchyard.integration._install', partial), self.assertRaises(Crash):
            self.integrate()
        return c, t

    def checkout_bytes(self):
        return {str(p.relative_to(self.target)): p.read_bytes() for p in sorted(self.target.rglob('*'))
                if p.is_file() and not p.is_symlink() and p.relative_to(self.target).parts[:2] != ('.git', 'objects')}

    def test_an_interrupted_install_without_a_lock_is_finished(self):
        self.interrupted_install(lock=False)
        seen = []; state = integration._checkout_state
        with patch('switchyard.integration._checkout_state', lambda *a: seen.append(state(*a)) or seen[-1]):
            r = self.integrate()
        self.assertEqual(seen[:2], ['partial', 'N'])  # Proven interrupted install of N, then verified at N.
        self.clean_at(r['integrated_commit_sha'])

    def test_an_existing_index_lock_is_never_removed_and_blocks_recovery(self):
        self.interrupted_install(lock=True)
        m = self.git('rev-parse', 'main'); lock = self.target/'.git/index.lock'; before = self.checkout_bytes()
        for _ in range(2):
            e = self.blocked(self.integrate)
            self.assertEqual((e.reason.split(':')[0], e.paths), ('target_worktree_locked', (str(self.target),)))
            self.assertEqual((lock.read_bytes(), self.git('rev-parse', 'main'), self.checkout_bytes(), self.history()),
                             (b'held by someone', m, before, ['intent']))
        lock.unlink()  # A human verified the lock is stale.
        r = self.integrate()
        self.assertEqual((r['integrated_commit_sha'], self.history()), (m, ['intent', 'integrated']))
        self.clean_at(m)

    # 16
    def test_crash_after_the_checkout_was_installed_before_the_final_record(self):
        c, t = self.drifted(); original = integration._finalize; calls = []
        def finalize(records, intent):
            calls.append(1)
            if len(calls) == 1:
                raise Crash()
            return original(records, intent)
        with patch('switchyard.integration._finalize', finalize):
            with self.assertRaises(Crash):
                self.integrate()
            m = self.git('rev-parse', 'main'); self.clean_at(m); stamp = (self.target/'keep.txt').stat().st_mtime_ns
            r = self.integrate()
        self.assertEqual((r['integrated_commit_sha'], self.history(), (self.target/'keep.txt').stat().st_mtime_ns), (m, ['intent', 'integrated'], stamp))

    # 17
    def test_local_changes_after_a_crash_are_never_reset(self):
        c, t = self.drifted()
        with patch('switchyard.integration._install', side_effect=Crash()), self.assertRaises(Crash):
            self.integrate()
        m = self.git('rev-parse', 'main')
        (self.target/'script.sh').write_text('user work after the crash\n')  # Not part of this integration.
        e = self.blocked(self.integrate)
        self.assertEqual((e.reason, e.integration['planned_commit_sha']),
                         ('integration_reconciliation_ambiguous: checkout holds changes that are not part of this integration', m))
        self.assertEqual(((self.target/'script.sh').read_text(), self.git('rev-parse', 'main'), self.history()),
                         ('user work after the crash\n', m, ['intent']))
        (self.target/'script.sh').write_text('#!/bin/sh\necho hi\n')  # Even a path N changes, with other bytes.
        (self.target/'source.py').write_text('user edit of an integrated path')
        self.assertTrue(self.blocked(self.integrate).reason.startswith('integration_reconciliation_ambiguous'))
        self.assertEqual((self.target/'source.py').read_text(), 'user edit of an integrated path')


class AlreadyIntegratedCheckoutTests(CheckoutBase):
    def test_already_integrated_reports_an_inconsistent_checkout_without_repairing_it(self):
        c = self.ready_checked_out(); carrier = self.root/'carrier'
        self.git('branch', 'carrier', c); self.git('worktree', 'add', '-q', str(carrier), 'carrier')
        (carrier/'keep.txt').write_text('local\n')
        self.assess('carrier'); r = self.integrate('carrier')
        self.assertEqual((r['integrated_by'], r['target_worktree'], r['target_worktree_clean'], r['target_worktree_problem']),
                         ('already_present', str(carrier), False, 'target_worktree_dirty'))
        self.assertEqual(((carrier/'keep.txt').read_text(), self.git('rev-parse', 'carrier')), ('local\n', c))


if __name__ == '__main__':
    unittest.main()
