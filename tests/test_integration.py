"""INTEGRATION (#9) slice 1: read-only, deterministic integration readiness of an accepted delivery commit."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from switchyard.continuity import transplant_v2_work
from switchyard.integration import integration_readiness, integration_status
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import attach_qa_candidate, accept_v2_candidate, recovery_status, RecoveryBlocked, _operation
from switchyard.suspend import suspend_v2_work
from switchyard.work_contract import WorkWorkspace

from test_continuity import ROOT
from test_continuity_transplant import TransplantBase
from test_delivery_suspend import LOGO

I = 'INTEGRATION_BLOCKED'
CONFLICTS = [dict(path='logo.png', kind='binary'), dict(path='obsolete.py', kind='delete/modify'),
             dict(path='source.py', kind='content'), dict(path='tool.sh', kind='add/add')]


class IntegrationBase(TransplantBase):
    def accepted(self):
        self.w1_ready()
        self.c = accept_v2_candidate(self.work).committed_head
        self.assertFalse((self.work/'integration').exists())  # Acceptance never assesses or records integration.
        return self.c

    def assess(self, target=None, work=None):
        return integration_readiness(work or self.work, target)

    def blocked(self, call):
        with self.assertRaises(RecoveryBlocked) as ctx:
            call()
        return ctx.exception

    def repository(self):
        """Every piece of repository state integration readiness must not change."""
        refs = self.git('for-each-ref', '--format=%(refname) %(objectname)')
        out = dict(refs=refs, worktrees=self.git('worktree', 'list', '--porcelain'), source=self.source_state())
        if self.delivery.exists():
            out['delivery'] = (self.files(self.delivery), self.git('status', '--porcelain=v2', '--untracked-files=all', cwd=self.delivery),
                               self.git('rev-parse', '--git-path', 'index', cwd=self.delivery))
            index = Path(out['delivery'][2]); out['delivery_index'] = index.read_bytes() if index.is_absolute() else (self.delivery/index).read_bytes()
        out['work'] = {str(p.relative_to(self.work)): p.read_bytes() for p in sorted(self.work.rglob('*'))
                       if p.is_file() and not p.is_symlink() and p.relative_to(self.work).parts[0] not in ('integration', 'workspace')}
        return out

    def records(self, work=None):
        return WorkWorkspace.open((work or self.work)/'integration').versions('integration_assessment')

    def blob(self, tree, path):
        return self.git('cat-file', 'blob', tree+':'+path)


class ReadinessTests(IntegrationBase):
    # 1, 2, 3, 19, 20, 21
    def test_unchanged_target_is_ready_and_nothing_in_the_repository_changes(self):
        c = self.accepted(); before = self.repository(); calls = self.model_calls()
        a = self.assess()
        self.assertEqual((a['readiness'], a['drift'], a['strategy'], a['conflicts']), ('READY', 'none', 'fast_forward', []))
        self.assertEqual((a['target_ref'], a['target_sha'], a['accepted_commit_sha'], a['delivery_base_sha'], a['work_id']),
                         ('refs/heads/main', self.base, c, self.base, 'work'))  # Default: the recorded source branch.
        self.assertEqual(a['result_tree'], self.git('rev-parse', c+'^{tree}'))
        self.assertEqual((a['delivery_branch_state'], a['stale'], a['current_target_sha']), ('at_accepted', False, self.base))
        explicit = self.assess('main')
        self.assertEqual((explicit['target_ref'], explicit['target_sha'], explicit['readiness']), ('main', self.base, 'READY'))
        self.assertEqual((self.assess(self.base)['target_sha'], self.assess(self.base)['target_ref']), (self.base, self.base))
        self.assertEqual(self.repository(), before)  # Refs, HEADs, indexes, files, worktrees, recovery history.
        self.assertEqual(self.model_calls(), calls)
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['accepted_commit_sha']), ('ACCEPTED', c))
        self.assertEqual((status['integration']['readiness'], status['integration']['target_sha'], status['integration']['stale']),
                         ('READY', self.base, False))

    # 4, 17, 18
    def test_clean_drift_is_ready_with_drift_deterministically_and_goes_stale_when_the_target_moves(self):
        c = self.accepted()
        t1 = self.advance(write={'keep.txt': 'upstream\n', 'upstream.bin': b'\x00U'}, message='T1')
        before = self.repository()
        a1 = self.assess()
        self.assertEqual((a1['readiness'], a1['drift'], a1['strategy'], a1['conflicts'], a1['target_sha']),
                         ('READY_WITH_DRIFT', 'clean', 'three_way', [], t1))
        tree = a1['result_tree']  # B's change and C's change, never committed or referenced.
        self.assertEqual((self.blob(tree, 'keep.txt'), self.blob(tree, 'source.py')), ('upstream', 'changed'))
        self.assertEqual(self.assess(), a1)  # Same inputs -> same identity, nothing new recorded.
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.repository(), before)
        t2 = self.advance(write={'keep.txt': 'later\n'}, message='T2')
        cached = integration_status(self.work)
        self.assertEqual((cached['target_sha'], cached['current_target_sha'], cached['stale'], cached['assessment_id']),
                         (t1, t2, True, a1['assessment_id']))
        self.assertEqual((recovery_status(self.work)['integration']['stale'], recovery_status(self.work)['integration']['target_sha']), (True, t1))
        a2 = self.assess()
        self.assertEqual((a2['target_sha'], a2['readiness'], a2['assessment_version'], a2['stale']), (t2, 'READY_WITH_DRIFT', 2, False))
        self.assertNotEqual(a2['assessment_id'], a1['assessment_id'])
        self.assertEqual(json.loads(WorkWorkspace.open(self.work/'integration').read_artifact('integration_assessment', 1))['target_sha'], t1)
        self.assertFalse(integration_status(self.work)['stale'])
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), c)  # No merge, no rebase, no branch update.
        self.assertEqual(self.git('rev-parse', 'main'), t2)

    # 5-8, 14, 22
    def test_conflicting_drift_is_blocked_with_the_transplant_engine_s_diagnostics(self):
        self.accepted(); calls = self.model_calls()
        t = self.advance(write={'source.py': 'upstream edit', 'logo.png': LOGO[1:], 'obsolete.py': 'upstream keeps it\n',
                                'tool.sh': '#!/bin/sh\necho upstream tool\n'})
        before = self.repository()
        a = self.assess()
        self.assertEqual((a['readiness'], a['drift'], a['strategy'], a['result_tree'], a['target_sha']),
                         ('BLOCKED_CONFLICT', 'conflicting', 'three_way', None, t))
        self.assertEqual(a['conflicts'], CONFLICTS)
        self.assertEqual(self.assess(), a)
        with self.assertRaises(RecoveryBlocked) as ctx:  # Same A, same change, same target -> same normalized conflicts.
            transplant_v2_work(self.work, self.w2, self.text, self.config)
        self.assertEqual(ctx.exception.transplant['conflicts'], a['conflicts'])
        self.assertEqual(self.repository(), before)
        self.assertEqual(self.model_calls(), calls)
        for root in (self.target, self.delivery):
            self.assertFalse([p for p in root.rglob('*') if p.is_file() and '.git' not in p.relative_to(root).parts
                              and b'<<<<<<<' in p.read_bytes()])
        self.reset_source(); self.advance(delete=['source.py'])
        self.assertEqual(self.assess()['conflicts'], [dict(path='source.py', kind='modify/delete')])

    # 9
    def test_unrelated_lineage_is_blocked_without_guessing(self):
        self.accepted()
        self.git('checkout', '-q', '--orphan', 'unrelated'); self.git('commit', '-qm', 'unrelated root'); self.git('checkout', '-q', 'main')
        a = self.assess('unrelated')
        self.assertEqual((a['readiness'], a['drift'], a['strategy'], a['conflicts']), ('BLOCKED_LINEAGE', 'unsupported_lineage', None, []))
        self.assertEqual(a['target_sha'], self.git('rev-parse', 'unrelated'))

    def test_a_target_that_already_contains_the_accepted_commit_is_already_integrated(self):
        c = self.accepted()
        self.git('branch', 'integrated', c)
        a = self.assess('integrated')
        self.assertEqual((a['readiness'], a['drift'], a['target_sha']), ('ALREADY_INTEGRATED', 'integrated', c))


class EligibilityTests(IntegrationBase):
    # 10
    def test_unknown_or_option_like_targets_are_not_found(self):
        self.accepted()
        for target in ('nope', 'refs/heads/missing', '--all', '0'*40):
            error = self.blocked(lambda: self.assess(target))
            self.assertEqual((error.terminal, error.reason, error.paths), (I, 'target_not_found', (target,)))

    # 11
    def test_only_accepted_isolated_work_is_assessed(self):
        self.w1_ready()
        error = self.blocked(self.assess)
        self.assertEqual((error.terminal, error.reason), (I, 'work_not_accepted: READY_FOR_MANUAL_QA'))
        w = self.root/'w-suspended'; self.work, self.delivery = w, w/'workspace'
        self.w1_ready(candidate=False); suspend_v2_work(w)
        self.assertEqual(self.blocked(self.assess).reason, 'work_not_accepted: SUSPENDED')
        self.assertEqual(self.blocked(lambda: integration_readiness(self.root/'nowhere')).reason, 'work_not_accepted: no recovery record')
        self.assertFalse((self.root/'work/integration').exists()); self.assertFalse((w/'integration').exists())

    # 12
    def test_broken_acceptance_provenance_is_refused_and_never_repaired(self):
        c = self.accepted(); store = WorkWorkspace.open(self.work/'recovery')
        op = json.loads(store.read_artifact('operation'))
        _operation(store, 'accepted', **{**{k: v for k, v in op.items() if k not in ('phase', 'timestamp')}, 'accepted_commit_sha': self.base})
        tampered = store.read_artifact('operation')
        error = self.blocked(self.assess)
        self.assertEqual(error.terminal, I); self.assertTrue(error.reason.startswith('accepted_commit_invalid'))
        self.assertEqual(store.read_artifact('operation'), tampered)
        _operation(store, 'accepted', **{k: v for k, v in op.items() if k not in ('phase', 'timestamp')})
        self.assertEqual(self.assess()['accepted_commit_sha'], c)
        loose = Path(self.git('rev-parse', '--git-path', 'objects/%s/%s' % (c[:2], c[2:])))
        loose = loose if loose.is_absolute() else self.target/loose
        content = loose.read_bytes(); loose.chmod(0o644); loose.unlink()  # C itself lost from the repository.
        try:
            self.assertEqual(self.blocked(self.assess).reason, 'accepted_commit_invalid: commit missing')
        finally:
            loose.write_bytes(content)
        self.assertEqual(self.assess()['readiness'], 'READY')


class IndependenceTests(IntegrationBase):
    # 13, 14
    def test_source_checkout_branch_and_dirt_are_irrelevant(self):
        self.accepted(); t = self.advance(write={'keep.txt': 'upstream\n'})
        expected = self.assess('main')
        self.git('checkout', '-q', '-b', 'elsewhere', self.base)
        (self.target/'source.py').write_text('uncommitted'); (self.target/'stray.txt').write_text('dirt'); self.git('add', 'stray.txt')
        before = self.repository()
        a = self.assess('main')
        self.assertEqual({k: a[k] for k in ('readiness', 'target_sha', 'assessment_id')}, {k: expected[k] for k in ('readiness', 'target_sha', 'assessment_id')})
        self.assertEqual(a['target_sha'], t)
        self.assertEqual(self.repository(), before)  # The dirt and the checked-out branch are left exactly as they were.

    # 23
    def test_manual_delivery_branch_movement_is_reported_but_c_stays_authoritative(self):
        c = self.accepted()
        self.git('update-ref', 'refs/heads/switchyard/work', self.base)  # Someone moves the delivery branch back.
        a = self.assess()
        self.assertEqual((a['accepted_commit_sha'], a['readiness'], a['delivery_branch_state'], a['delivery_branch_sha']),
                         (c, 'READY', 'behind', self.base))
        self.assertEqual(self.git('rev-parse', 'switchyard/work'), self.base)  # Never reset.
        self.git('update-ref', '-d', 'refs/heads/switchyard/work')
        self.assertEqual((self.assess()['delivery_branch_state'], self.assess()['accepted_commit_sha']), ('missing', c))

    # 15, 16
    def test_transplanted_accepted_work_is_assessed_from_its_own_base(self):
        self.w1_ready(); b = self.advance(write={'keep.txt': 'B\n'})
        execution = transplant_v2_work(self.work, self.w2, self.text, self.config)
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        c2 = accept_v2_candidate(self.w2).committed_head
        a = self.assess(work=self.w2)
        self.assertEqual((a['readiness'], a['delivery_base_sha'], a['target_sha'], a['accepted_commit_sha']), ('READY', b, b, c2))
        self.assertFalse([k for k in a if k.startswith(('adopt', 'transplant', 'derived', 'source_'))])
        d = self.advance(write={'upstream.txt': 'D\n'}, message='D')
        a = self.assess(work=self.w2)
        self.assertEqual((a['readiness'], a['delivery_base_sha'], a['target_sha']), ('READY_WITH_DRIFT', b, d))
        self.assertEqual(recovery_status(self.w2)['transplant_from_base_sha'], self.base)  # Provenance still inspectable.


class CliTests(IntegrationBase):
    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'switchyard', 'work', 'integration-status', str(self.work), *args],
                              cwd=ROOT, capture_output=True, text=True, env={**self.config.environment})

    def test_cli_reports_readiness_conflicts_and_refusals(self):
        c = self.accepted(); t = self.advance(write={'keep.txt': 'upstream\n'})
        out = self.cli('--target', 'main')
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[:7], ['Integration READY_WITH_DRIFT', 'Target: main @ '+t, 'Accepted: '+c, 'Base: '+self.base,
                                     'Drift: clean', 'Strategy: three-way', 'Conflicts: none'])
        self.assertTrue(lines[7].startswith('Assessment: '))
        structured = json.loads(self.cli('--target', 'main', '--json').stdout)
        self.assertEqual((structured['readiness'], structured['target_sha']), ('READY_WITH_DRIFT', t))
        self.reset_source(); self.advance(write={'source.py': 'upstream edit'})
        out = self.cli()
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout.splitlines()[0], 'Integration BLOCKED_CONFLICT')
        self.assertIn('Conflicts:\n  source.py - content', out.stdout)
        out = self.cli('--target', 'nope')
        self.assertEqual(out.returncode, 2)
        self.assertIn('INTEGRATION_BLOCKED: target_not_found', out.stderr)


if __name__ == '__main__':
    unittest.main()
