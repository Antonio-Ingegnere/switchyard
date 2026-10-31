"""CONTINUITY (#5) slice 3: deterministic 3-way transplant of W1's implementation onto a descendant base."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from switchyard import continuity, recovery
from switchyard.continuity import adopt_v2_work, continuity_plan, implementation_digest
from switchyard.observability import read_events
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (execute_recoverable_v2_work, attach_qa_candidate, accept_v2_candidate, recovery_status,
                                 RecoveryBlocked, _operation)
from switchyard.suspend import suspend_v2_work
from switchyard.work_contract import WorkWorkspace

from test_continuity import ContinuityBase
from test_delivery_isolation import Crash, crash_once, REQUEST
from test_delivery_suspend import LOGO


class TransplantBase(ContinuityBase):
    def advance(self, write=None, delete=(), modes=None, message='B'):
        """One commit on the source checkout: the target base B."""
        for name, content in (write or {}).items():
            path = self.target/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode())
        for name in delete:
            (self.target/name).unlink()
        for name, mode in (modes or {}).items():
            (self.target/name).chmod(mode)
        self.git('add', '-A'); self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def reset_source(self):
        self.git('reset', '-q', '--hard', self.base)

    def implement(self, plan):
        return next(d for d in plan['decisions'] if d.get('product') == 'implementation')


class CleanTransplantTests(TransplantBase):
    # 2-10, 16, 17, 31, 32
    def test_clean_transplant_onto_a_descendant_base_then_verify_and_accept(self):
        execution = self.w1_ready(); w1 = self.w1_bytes(); calls = self.model_calls()
        b = self.advance(write={'keep.txt': 'upstream\n', 'upstream.bin': b'\x00\x9fB', 'script.sh': '#!/bin/sh\necho upstream\n'})
        execution = self.adopt()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual(self.model_calls(), calls)  # Transplant and adoption never invoke a model.
        status = recovery_status(self.w2)
        self.assertEqual((status['base_sha'], status['adopted_from_base_sha'], status['transplanted'],
                          status['transplant_from_base_sha'], status['transplant_to_base_sha']), (b, self.base, True, self.base, b))
        self.assertEqual(self.git('rev-parse', 'switchyard/w2'), b)
        d2 = self.d2
        self.assertEqual((d2/'source.py').read_text(), 'changed')                     # W1 implementation
        self.assertEqual((d2/'logo.png').read_bytes(), LOGO[::-1])                  # W1 binary modification
        self.assertFalse((d2/'obsolete.py').exists())                               # W1 deletion
        self.assertTrue(os.access(d2/'tool.sh', os.X_OK))                           # W1 new executable
        self.assertEqual((d2/'keep.txt').read_text(), 'upstream\n')                  # unrelated B change
        self.assertEqual((d2/'upstream.bin').read_bytes(), b'\x00\x9fB')             # unrelated B binary add
        self.assertEqual((d2/'script.sh').read_text(), '#!/bin/sh\necho upstream\n')  # B content + W1 mode-only change
        self.assertTrue(os.access(d2/'script.sh', os.X_OK))
        plan = continuity_plan(self.w2); implement = self.implement(plan)
        adoption = json.loads(WorkWorkspace.open(self.w2/'recovery').read_artifact('baseline'))['adoption']
        self.assertEqual((implement['action'], implement['reason']), ('REUSED', 'deterministic_three_way_transplant'))
        transplant = implement['transplant']
        self.assertEqual((transplant['strategy'], transplant['from_base'], transplant['to_base'], transplant['result_digest']),
                         ('git_merge_tree_three_way', self.base, b, implementation_digest(adoption['changes'])))
        self.assertNotEqual(transplant['result_digest'], implement['implementation']['digest'])  # Not claimed byte-identical.
        self.assertEqual((plan['base_sha'], plan['target_base_sha']), (self.base, b))
        self.assertEqual([(d['stage'], d['action']) for d in plan['decisions']][-1], ('VERIFY', 'RERUN'))
        events = tuple(read_events(self.w2/'events.jsonl'))
        self.assertEqual([e.state.split(':')[2] for e in events if e.kind == 'invocation_started'], ['VERIFY'])
        self.assertEqual(self.w1_bytes(), w1)
        create_qa_candidate(d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2')
        commit = accept_v2_candidate(self.w2).committed_head
        self.assertEqual(self.git('rev-parse', commit+'^'), b)  # B -> C2, never A -> C2.
        self.assertEqual(self.w1_bytes(), w1)

    # 1, 30
    def test_same_base_adoption_is_unchanged(self):
        self.w1_ready(); self.adopt()
        status = recovery_status(self.w2); plan = continuity_plan(self.w2)
        self.assertEqual((status['transplanted'], status['base_sha']), (False, self.base))
        self.assertNotIn('target_base_sha', plan)
        self.assertEqual(self.implement(plan)['reason'], 'durable_product_available')
        self.assertNotIn('transplant', self.implement(plan))


class TransplantConflictTests(TransplantBase):
    # 11-15, 19, 20, 32
    def test_conflicts_block_with_deterministic_normalized_diagnostics(self):
        self.w1_ready(); w1 = self.w1_bytes(); calls = self.model_calls()
        b = self.advance(write={'source.py': 'upstream edit', 'logo.png': LOGO[1:], 'obsolete.py': 'upstream keeps it\n',
                                'tool.sh': '#!/bin/sh\necho upstream tool\n'})
        diagnostics = []
        for _ in range(2):
            with self.assertRaises(RecoveryBlocked) as ctx:
                self.adopt()
            self.assertEqual((ctx.exception.terminal, ctx.exception.reason), ('ADOPTION_BLOCKED', 'transplant_conflict'))
            diagnostics.append((ctx.exception.paths, ctx.exception.transplant, ctx.exception.continuity_plan))
        self.assertEqual(diagnostics[0], diagnostics[1])  # Same A, B, implementation -> same conflict set.
        paths, transplant, plan = diagnostics[0]
        self.assertEqual(transplant['conflicts'], [dict(path='logo.png', kind='binary'), dict(path='obsolete.py', kind='delete/modify'),
                                                   dict(path='source.py', kind='content'), dict(path='tool.sh', kind='add/add')])
        self.assertEqual(paths, ('logo.png', 'obsolete.py', 'source.py', 'tool.sh'))
        self.assertEqual((transplant['from_base'], transplant['to_base'], transplant['implementation']['source']), (self.base, b, 'candidate:1'))
        self.assertEqual((self.implement(plan)['action'], self.implement(plan)['reason']), ('INVALIDATED', 'transplant_conflict'))
        self.assertFalse((self.w2/'recovery').exists())                   # No W2 at all, let alone a conflicted one.
        self.assertEqual(self.branches(), ['refs/heads/switchyard/work'])
        self.assertEqual((self.w1_bytes(), self.model_calls()), (w1, calls))
        self.reset_source(); self.advance(delete=['source.py'])
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.transplant['conflicts'], [dict(path='source.py', kind='modify/delete')])

    # 21
    def test_artifact_contract_invalidation_wins_before_transplant(self):
        self.w1_ready(); self.advance(write={'source.py': 'would conflict'})
        data = json.loads(self.text); data['flows']['feature']['states']['IMPLEMENT']['produces']['implementation_handoff'] = dict(format='text')
        with self.assertRaises(RecoveryBlocked) as ctx:
            adopt_v2_work(self.work, self.w2, json.dumps(data), self.config)
        self.assertEqual(ctx.exception.reason, 'continuity_invalidated: IMPLEMENT')
        self.assertIsNone(getattr(ctx.exception, 'transplant', None))

    # 22, 23, 24
    def test_unrelated_repository_lineage_and_dirty_target_are_refused(self):
        self.w1_ready()
        other = self.root/'other'; shutil.copytree(self.target, other); shutil.rmtree(other/'.git')
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'independent')):
            self.git(*args, cwd=other)
        with self.assertRaises(RecoveryBlocked) as ctx:
            adopt_v2_work(self.work, self.w2, self.text, self.provider(other))
        self.assertEqual(ctx.exception.reason, 'repository_mismatch')
        self.git('checkout', '-q', '--orphan', 'unrelated'); self.git('commit', '-qm', 'unrelated root')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'incompatible_base_lineage')
        self.git('checkout', '-q', 'main'); self.advance(write={'keep.txt': 'B\n'}); (self.target/'stray.txt').write_text('dirt')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertTrue(ctx.exception.reason.startswith('dirty_source_worktree'))
        self.assertFalse((self.w2/'recovery').exists())


class TransplantRecoveryTests(TransplantBase):
    # 25, 26
    def test_crash_after_intent_then_target_moves_keeps_the_recorded_base(self):
        self.w1_ready(); b = self.advance(write={'keep.txt': 'B\n'})
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.adopt()
        written = continuity_plan(self.w2)
        self.advance(write={'keep.txt': 'D\n'}, message='D')  # The user keeps working on main.
        self.assertEqual(self.adopt().result.terminal_state, 'READY_FOR_MANUAL_QA')
        self.assertEqual((recovery_status(self.w2)['base_sha'], self.git('rev-parse', 'switchyard/w2')), (b, b))
        self.assertEqual((self.d2/'keep.txt').read_text(), 'B\n')
        self.assertEqual(continuity_plan(self.w2), written)
        self.assertEqual(sorted(self.branches()), ['refs/heads/switchyard/w2', 'refs/heads/switchyard/work'])

    # 27
    def test_crash_during_materialization_retries_to_the_identical_result(self):
        self.w1_ready(); self.advance(write={'keep.txt': 'B\n', 'upstream.bin': b'\x01'})
        calls = {'n': 0}; original = continuity._write_change
        def write(*args):
            calls['n'] += 1
            if calls['n'] == 3:
                raise Crash()
            return original(*args)
        with patch('switchyard.continuity._write_change', write), self.assertRaises(Crash):
            self.adopt()
        written = continuity_plan(self.w2); (self.d2/'debris').write_text('x')
        self.adopt()
        self.assertEqual(continuity_plan(self.w2), written)
        self.assertFalse((self.d2/'debris').exists()); self.assertEqual((self.d2/'source.py').read_text(), 'changed')

    # 28
    def test_retry_refuses_a_different_transplant_result(self):
        self.w1_ready(); self.advance(write={'keep.txt': 'B\n'})
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.adopt()
        store = WorkWorkspace.open(self.w2/'recovery'); op = json.loads(store.read_artifact('operation'))
        tampered = json.loads(json.dumps(op['adoption']))
        self.implement(tampered['continuity'])['transplant']['result_digest'] = '0'*64
        _operation(store, 'provisioning', isolation=op['isolation'], adoption=tampered)
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'continuity_plan_differs: retry with the original configuration')


class TransplantSourceFormTests(TransplantBase):
    # 18, 29
    def test_accepted_suspended_and_candidate_sources_transplant_equivalently(self):
        forms = {}
        for form in ('candidate', 'accepted', 'suspended'):
            w1 = self.root/('w1-'+form)
            execution = execute_recoverable_v2_work(self.text, 'feature', REQUEST, w1, self.config)
            d = w1/'workspace'
            (d/'logo.png').write_bytes(LOGO[::-1]); (d/'obsolete.py').unlink(); (d/'notes').mkdir(); (d/'notes/new.bin').write_bytes(b'\x00n')
            if form != 'suspended':
                create_qa_candidate(d, execution.workspace, execution.result, 'feature', self.root/('qa-'+form))
                attach_qa_candidate(w1, self.root/('qa-'+form))
            if form == 'accepted':
                accept_v2_candidate(w1)
            if form == 'suspended':
                (d/'link').symlink_to('keep.txt'); suspend_v2_work(w1)
            forms[form] = w1
        b = self.advance(write={'keep.txt': 'B\n'})
        plans = {}
        for form, w1 in forms.items():
            w2 = self.root/('w2-'+form)
            adopt_v2_work(w1, w2, self.text, self.config)
            plans[form] = continuity_plan(w2)
            self.assertEqual((recovery_status(w2)['base_sha'], (w2/'workspace/keep.txt').read_text()), (b, 'B\n'))
        logical = {f: [(d['stage'], d['action'], d['reason']) for d in p['decisions']] for f, p in plans.items()}
        self.assertEqual(logical['candidate'], logical['accepted']); self.assertEqual(logical['candidate'], logical['suspended'])
        digests = {f: self.implement(p)['transplant']['result_digest'] for f, p in plans.items()}
        self.assertEqual(digests['candidate'], digests['accepted'])  # Same delta, same result.
        self.assertNotEqual(digests['candidate'], digests['suspended'])  # The shelf also carries a symlink.
        self.assertEqual(os.readlink(self.root/'w2-suspended/workspace/link'), 'keep.txt')  # Link, not dereferenced.


if __name__ == '__main__':
    unittest.main()
