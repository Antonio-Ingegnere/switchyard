"""INTEGRATION (#9) slice 2: compare-and-swap integration consuming a fresh readiness assessment."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from switchyard import integration
from switchyard.continuity import transplant_v2_work
from switchyard.integration import integrate_v2_work, integration_readiness, integration_record, _IDENTITY, _canonical
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import attach_qa_candidate, accept_v2_candidate, recovery_status, _operation
from switchyard.work_contract import WorkWorkspace

from test_continuity import ROOT
from test_delivery_isolation import Crash
from test_delivery_suspend import LOGO
from test_integration import IntegrationBase, I


class IntegrateBase(IntegrationBase):
    def source_state(self):
        files = {str(p.relative_to(self.target)): p.read_bytes() for p in self.target.rglob('*')
                 if p.is_file() and '.git' not in p.relative_to(self.target).parts}
        return dict(head=self.git('rev-parse', 'HEAD'), branch=self.git('rev-parse', '--symbolic-full-name', 'HEAD'),
                    index=(self.target/'.git/index').read_bytes(), files=files,
                    status=self.git('status', '--porcelain', '--untracked-files=all'))

    def release(self):
        """The source checkout leaves `main` (detached), so `main` is not checked out anywhere."""
        self.git('checkout', '-q', '--detach')

    def commit_main(self, write, message='T'):
        """Advance `main` without checking it out anywhere."""
        self.git('checkout', '-q', 'main'); sha = self.advance(write=write, message=message); self.release()
        return sha

    def integrate(self, target=None, work=None):
        return integrate_v2_work(work or self.work, target)

    def history(self, work=None):
        return [r['phase'] for r in integration._history(integration._records(work or self.work))]

    def commits(self):
        return int(self.git('rev-list', '--all', '--count'))

    def parents(self, sha):
        return self.git('rev-list', '--parents', '-n', '1', sha).split()[1:]


class FastForwardTests(IntegrateBase):
    # 1, 2, 7, 21, 24-28
    def test_ready_fast_forwards_the_target_to_the_accepted_commit(self):
        c = self.accepted(); self.assertEqual(self.git('rev-parse', 'main'), self.base)  # Accept never touches main.
        self.release(); a = self.assess(); calls = self.model_calls(); commits = self.commits()
        assessment = WorkWorkspace.open(self.work/'integration').read_artifact('integration_assessment')
        r = self.integrate()
        self.assertEqual((r['phase'], r['integrated_by'], r['strategy'], r['target_ref_full'], r['target_before_sha'],
                          r['target_after_sha'], r['integrated_commit_sha'], r['accepted_commit_sha'], r['consumed_assessment_id']),
                         ('integrated', 'switchyard', 'fast_forward', 'refs/heads/main', self.base, c, c, c, a['assessment_id']))
        self.assertEqual((self.git('rev-parse', 'main'), self.commits()), (c, commits))  # No merge commit.
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), c)  # Delivery branch untouched.
        self.assertEqual(self.model_calls(), calls)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['accepted_commit_sha']), ('ACCEPTED', c))
        self.assertEqual({k: status['integration'][k] for k in ('state', 'integrated_by', 'strategy', 'integrated_commit_sha', 'target_contains_integration')},
                         dict(state='INTEGRATED', integrated_by='switchyard', strategy='fast_forward', integrated_commit_sha=c, target_contains_integration=True))
        self.assertEqual(WorkWorkspace.open(self.work/'integration').read_artifact('integration_assessment', 1), assessment)  # Never rewritten.
        before = self.repository()
        self.assertEqual(self.integrate(), r)  # Idempotent: nothing moves, nothing recorded.
        self.assertEqual((self.history(), self.repository()), (['intent', 'integrated'], before))
        self.assertEqual(self.assess()['readiness'], 'ALREADY_INTEGRATED')  # Readiness stays read-only.
        self.assertEqual(self.repository(), before)


class MergeTests(IntegrateBase):
    # 3-6, 23, 29 (provenance)
    def test_clean_drift_creates_one_deterministic_merge_commit_with_the_assessed_tree(self):
        c = self.accepted(); self.release()
        t = self.commit_main({'keep.txt': 'upstream\n'})
        a = self.assess(); commits = self.commits()
        r = self.integrate()
        m = r['integrated_commit_sha']
        self.assertEqual((r['strategy'], r['target_before_sha'], r['target_after_sha']), ('merge', t, m))
        self.assertEqual((self.parents(m), self.git('rev-parse', m+'^{tree}'), self.git('rev-parse', 'main')), ([t, c], a['result_tree'], m))
        self.assertEqual(self.commits(), commits+1)
        body = self.git('cat-file', 'commit', m)
        self.assertIn('\n\nswitchyard: integrate work work into refs/heads/main\n\nSwitchyard-Work: work\nSwitchyard-Accepted-Commit: %s\n'
                      'Switchyard-Assessment: %s' % (c, a['assessment_id']), body)
        self.assertIn('author Switchyard <switchyard@invalid> 0 +0000', body)  # Fixed identity/epoch: deterministic M.
        self.assertEqual((self.blob(m, 'keep.txt'), self.blob(m, 'source.py')), ('upstream', 'changed'))
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), c)
        n = self.commit_main({'later.txt': 'N\n'}, 'N')  # Normal later movement keeps the provenance.
        status = recovery_status(self.work)['integration']
        self.assertEqual((status['state'], status['integrated_commit_sha'], status['current_target_sha'], status['target_contains_integration']),
                         ('INTEGRATED', m, n, True))
        self.assertEqual(self.integrate(), r)

    # 29
    def test_transplanted_accepted_work_integrates_from_its_own_base(self):
        self.w1_ready(); b = self.advance(write={'keep.txt': 'B\n'})
        execution = transplant_v2_work(self.work, self.w2, self.text, self.config)
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        c2 = accept_v2_candidate(self.w2).committed_head
        self.release(); d = self.commit_main({'upstream.txt': 'D\n'}, 'D')
        a = self.assess(work=self.w2)
        self.assertEqual((a['readiness'], a['delivery_base_sha']), ('READY_WITH_DRIFT', b))
        r = self.integrate(work=self.w2)
        self.assertEqual((self.parents(r['integrated_commit_sha']), r['delivery_base_sha'], r['work_id']), ([d, c2], b, 'w2'))

    # 30
    def test_merge_provenance_uses_the_durable_work_id_not_the_directory_name(self):
        c = self.accepted(); self.release(); self.commit_main({'keep.txt': 'upstream\n'})
        moved = self.root/'renamed-work'; os.rename(self.work, moved)
        a = integration_readiness(moved, None)
        r = integrate_v2_work(moved, None)
        self.assertEqual((a['work_id'], r['work_id']), ('work', 'work'))
        self.assertIn('Switchyard-Work: work\n', self.git('cat-file', 'commit', r['integrated_commit_sha']))
        self.assertNotIn('renamed-work', self.git('cat-file', 'commit', r['integrated_commit_sha']))

    # 22
    def test_already_integrated_target_records_presence_without_creating_anything(self):
        c = self.accepted(); self.git('branch', 'carrier', c); commits = self.commits()
        self.assess('carrier')
        r = self.integrate('carrier')
        self.assertEqual((r['integrated_by'], r['integrated_commit_sha'], r['already_integrated_at'], r['strategy']),
                         ('already_present', None, c, None))
        self.assertEqual((self.git('rev-parse', 'carrier'), self.commits(), self.history()), (c, commits, ['integrated']))


class SafetyTests(IntegrateBase):
    # 8
    def test_a_checked_out_target_is_never_moved_behind_the_checkout(self):
        self.accepted(); self.assess()
        e = self.blocked(self.integrate)
        self.assertEqual((e.terminal, e.reason, e.paths), (I, 'target_checked_out', (str(self.target),)))
        self.release(); other = self.root/'other-wt'
        self.git('worktree', 'add', '-q', str(other), 'main')
        e = self.blocked(self.integrate)
        self.assertEqual((e.reason, e.paths), ('target_checked_out', (str(other),)))
        self.assertEqual((self.git('rev-parse', 'main'), self.history()), (self.base, []))
        self.git('worktree', 'remove', str(other))
        self.assertEqual(self.integrate()['strategy'], 'fast_forward')

    # 9
    def test_dirty_unrelated_source_checkout_does_not_matter(self):
        c = self.accepted(); self.git('checkout', '-q', '-b', 'elsewhere')
        (self.target/'source.py').write_text('uncommitted'); (self.target/'stray.txt').write_text('dirt')
        source = self.source_state()
        self.assess(); r = self.integrate()
        self.assertEqual((r['strategy'], self.git('rev-parse', 'main'), self.source_state()), ('fast_forward', c, source))

    # 10, 11
    def test_a_moved_target_blocks_until_reassessed_and_is_never_reassessed_implicitly(self):
        c = self.accepted(); self.release()
        t1 = self.commit_main({'keep.txt': 'T1\n'}, 'T1'); self.assess()
        t2 = self.commit_main({'keep.txt': 'T2\n'}, 'T2')
        e = self.blocked(self.integrate)
        self.assertEqual((e.reason, e.integration), ('target_moved', dict(expected_target_sha=t1, actual_target_sha=t2)))
        self.assertEqual((self.git('rev-parse', 'main'), self.history()), (t2, []))
        self.assertEqual(self.assess()['target_sha'], t2)
        self.assertEqual(self.parents(self.integrate()['integrated_commit_sha']), [t2, c])

    # 12
    def test_a_lost_compare_and_swap_race_blocks_and_records_the_abort(self):
        self.accepted(); self.release(); self.commit_main({'keep.txt': 'T\n'}); self.assess()
        original = integration._cas
        def racing(repo, full, new, expected, work_id):
            self.git('update-ref', full, self.base)  # Another process wins between the check and the swap.
            return original(repo, full, new, expected, work_id)
        with patch('switchyard.integration._cas', racing):
            e = self.blocked(self.integrate)
        self.assertEqual((e.reason, e.integration['actual_target_sha']), ('target_moved', self.base))
        self.assertEqual((self.git('rev-parse', 'main'), self.history()), (self.base, ['intent', 'aborted']))
        self.assertEqual(recovery_status(self.work)['integration']['last_integration_attempt'], 'aborted: target_moved')
        self.assertEqual(self.blocked(self.integrate).reason, 'target_moved')  # Still the old assessment: reassess first.

    # 13, 14
    def test_blocked_assessments_cannot_integrate(self):
        self.accepted(); self.release()
        self.commit_main({'source.py': 'upstream edit', 'logo.png': LOGO[1:]})
        self.assertEqual(self.assess()['readiness'], 'BLOCKED_CONFLICT')
        self.assertEqual(self.blocked(self.integrate).reason, 'assessment_not_ready: BLOCKED_CONFLICT')
        self.git('checkout', '-q', '--orphan', 'unrelated'); self.git('commit', '-qm', 'root'); self.release()
        self.assertEqual(self.assess('unrelated')['readiness'], 'BLOCKED_LINEAGE')
        self.assertEqual(self.blocked(lambda: self.integrate('unrelated')).reason, 'assessment_not_ready: BLOCKED_LINEAGE')
        self.assertEqual(self.history(), [])

    # 15, 16
    def test_missing_mismatched_or_non_branch_targets_are_refused(self):
        c = self.accepted(); self.release(); self.git('branch', 'other', self.base)
        self.assertEqual(self.blocked(self.integrate).reason, 'assessment_missing: run work integration-status first')
        self.assess()
        e = self.blocked(lambda: self.integrate('other'))
        self.assertEqual((e.reason, e.paths), ('assessment_target_mismatch', ('refs/heads/main',)))
        self.assertEqual(self.blocked(lambda: self.integrate(self.base)).reason, 'target_not_a_branch')
        self.assertEqual(self.blocked(lambda: self.integrate('nope')).reason, 'target_not_found')
        self.assertEqual(self.integrate('refs/heads/main')['integrated_commit_sha'], c)  # Same branch, other spelling.

    # 17
    def test_a_changed_accepted_product_is_refused(self):
        self.accepted(); self.release(); self.assess()
        records = WorkWorkspace.open(self.work/'integration')
        forged = json.loads(records.read_artifact('integration_assessment')); forged['accepted_commit_sha'] = self.base
        forged['assessment_id'] = hashlib.sha256(_canonical({k: forged[k] for k in _IDENTITY})).hexdigest()
        records.write_artifact('integration_assessment', json.dumps(forged))
        self.assertEqual(self.blocked(self.integrate).reason, 'assessment_product_changed')
        store = WorkWorkspace.open(self.work/'recovery'); op = json.loads(store.read_artifact('operation'))
        _operation(store, 'accepted', **{**{k: v for k, v in op.items() if k not in ('phase', 'timestamp')}, 'accepted_commit_sha': self.base})
        self.assertTrue(self.blocked(self.integrate).reason.startswith('accepted_commit_invalid'))
        self.assertEqual((self.git('rev-parse', 'main'), self.history()), (self.base, []))


class CrashTests(IntegrateBase):
    def drifted(self):
        c = self.accepted(); self.release(); t = self.commit_main({'keep.txt': 'T\n'}); self.assess()
        return c, t

    # 18
    def test_crash_after_the_merge_object_or_the_intent_reuses_the_same_m(self):
        c, t = self.drifted(); made = []; original = integration._merge_commit
        def merge(repo, intent, write):
            sha = original(repo, intent, write); made.append(sha); return sha
        with patch('switchyard.integration._merge_commit', merge), patch('switchyard.integration._put', side_effect=Crash()), \
                self.assertRaises(Crash):
            self.integrate()  # M object written, no intent yet.
        self.assertEqual((self.git('rev-parse', 'main'), self.history()), (t, []))
        with patch('switchyard.integration._merge_commit', merge), patch('switchyard.integration._cas', side_effect=Crash()), \
                self.assertRaises(Crash):
            self.integrate()  # Intent durable, CAS not done.
        self.assertEqual((self.git('rev-parse', 'main'), self.history()), (t, ['intent']))
        self.assertEqual(recovery_status(self.work)['integration']['state'], 'INTEGRATION_PENDING')
        with patch('switchyard.integration._merge_commit', merge):
            r = self.integrate()
        self.assertEqual(len(set(made)), 1)  # Every attempt computed the same M.
        self.assertEqual((r['integrated_commit_sha'], self.git('rev-parse', 'main'), self.history()), (made[0], made[0], ['intent', 'integrated']))

    # 19
    def test_crash_after_the_ref_moved_finalizes_without_a_second_commit(self):
        c, t = self.drifted(); original = integration._cas
        def moved_then_die(*args):
            assert original(*args); raise Crash()
        with patch('switchyard.integration._cas', moved_then_die), self.assertRaises(Crash):
            self.integrate()
        m = self.git('rev-parse', 'main'); commits = self.commits()
        self.assertEqual((self.parents(m), self.history()), ([t, c], ['intent']))
        r = self.integrate()
        self.assertEqual((r['integrated_commit_sha'], r['target_before_sha'], self.git('rev-parse', 'main'), self.commits()), (m, t, m, commits))
        self.assertEqual(self.history(), ['intent', 'integrated'])

    # 20
    def test_an_ambiguous_target_after_a_crash_is_refused_without_reset(self):
        c, t = self.drifted()
        with patch('switchyard.integration._cas', side_effect=Crash()), self.assertRaises(Crash):
            self.integrate()
        x = self.commit_main({'keep.txt': 'X\n'}, 'X')
        e = self.blocked(self.integrate)
        self.assertEqual((e.reason, e.integration['expected_target_sha'], e.integration['actual_target_sha']),
                         ('integration_reconciliation_ambiguous', t, x))
        self.assertEqual((self.git('rev-parse', 'main'), self.history()), (x, ['intent']))


class CliTests(IntegrateBase):
    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'switchyard', 'work', *args], cwd=ROOT, capture_output=True, text=True,
                              env={**self.config.environment})

    def test_cli_integrates_and_reports_target_movement(self):
        c = self.accepted(); self.release(); t1 = self.commit_main({'keep.txt': 'T1\n'}, 'T1')
        self.assertEqual(self.cli('integration-status', str(self.work)).returncode, 0)
        t2 = self.commit_main({'keep.txt': 'T2\n'}, 'T2')
        out = self.cli('integrate', str(self.work))
        self.assertEqual(out.returncode, 2)
        self.assertIn('INTEGRATION_BLOCKED: target_moved', out.stderr)
        self.assertEqual(json.loads(out.stderr.splitlines()[1]), dict(expected_target_sha=t1, actual_target_sha=t2))
        self.cli('integration-status', str(self.work))
        out = self.cli('integrate', str(self.work))
        self.assertEqual(out.returncode, 0, out.stderr)
        m = self.git('rev-parse', 'main'); lines = out.stdout.splitlines()
        self.assertEqual(lines[:5], ['Integration INTEGRATED', 'Target: refs/heads/main %s -> %s' % (t2, m), 'Strategy: merge',
                                     'Accepted: '+c, 'Integrated commit: '+m])
        self.assertEqual(json.loads(self.cli('integrate', str(self.work), '--json').stdout)['integrated_commit_sha'], m)


if __name__ == '__main__':
    unittest.main()
