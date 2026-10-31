"""CONTINUITY (#5) slice 2: explicit, deterministic, durable REUSED / RERUN / INVALIDATED decisions."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from switchyard import recovery
from switchyard.continuity import adopt_v2_work, continuity_plan, implementation_digest
from switchyard.observability import read_events
from switchyard.qa_lifecycle import create_qa_candidate
from switchyard.recovery import (execute_recoverable_v2_work, attach_qa_candidate, accept_v2_candidate, recovery_status,
                                 RecoveryBlocked, _operation)
from switchyard.suspend import suspend_v2_work
from switchyard.work_contract import WorkWorkspace

from test_continuity import ContinuityBase
from test_delivery_isolation import Crash, crash_once
from test_source_resolution import Transport, URL

ROOT = Path(__file__).resolve().parents[1]
LOGICAL = [('REQUEST', 'REUSED', 'exact_request'), ('SOURCE_RESOLUTION', 'REUSED', 'frozen_sources'),
           ('SPECIFY', 'REUSED', 'durable_product_available'), ('IMPLEMENT', 'REUSED', 'durable_product_available'),
           ('VERIFY', 'RERUN', 'current_runtime_verification_required')]


def logical(plan):
    return [(d['stage'], d['action'], d['reason']) for d in plan['decisions']]


class PlanBase(ContinuityBase):
    def start(self):
        with patch('switchyard.github_sources.http_get', Transport()):  # W1 resolves its sources once.
            return execute_recoverable_v2_work(self.text, 'feature', URL, self.work, self.config)

    def adopt(self, config=None, text=None):
        with patch('switchyard.github_sources.http_get', side_effect=AssertionError('sources are reused, never re-fetched')):
            return adopt_v2_work(self.work, self.w2, text or self.text, config or self.config)

    def w2_plan(self):
        return continuity_plan(self.w2)


class ContinuityPlanTests(PlanBase):
    # 1-11
    def test_happy_path_decisions_identities_status_and_truthful_history(self):
        self.w1_ready(); w1 = self.w1_bytes(); calls = self.model_calls()
        execution = self.adopt()
        plan = self.w2_plan()
        self.assertEqual(logical(plan), LOGICAL)
        self.assertEqual((plan['schema'], plan['source_work_id'], plan['base_sha'], plan['flow']), (1, 'work', self.base, 'feature'))
        old = WorkWorkspace.open(self.work); sources = WorkWorkspace.open(self.work/'sources')
        decisions = {d['stage']: d for d in plan['decisions']}
        ident = lambda ws, name: dict(artifact=name, version=ws.latest_artifact(name).version, sha256=ws.latest_artifact(name).sha256)
        self.assertEqual(decisions['REQUEST']['artifacts'], [ident(old, 'request')])
        self.assertEqual(decisions['SOURCE_RESOLUTION']['artifacts'], [ident(sources, 'resolved_sources')])
        self.assertEqual([{k: a[k] for k in ('artifact', 'version', 'sha256')} for a in decisions['SPECIFY']['artifacts']], [ident(old, 'microspec')])
        self.assertEqual(decisions['SPECIFY']['artifacts'][0]['contract'], dict(format='yaml', schema='microspec', source=None))
        implement = decisions['IMPLEMENT']
        adoption = json.loads(WorkWorkspace.open(self.w2/'recovery').read_artifact('baseline'))['adoption']
        self.assertEqual((implement['product'], implement['implementation']),
                         ('implementation', dict(source='candidate:1', identity=recovery_status(self.work)['qa_candidate']['identity'],
                                                 digest=implementation_digest(adoption['changes']), paths=len(adoption['changes']))))
        self.assertEqual([a['artifact'] for a in implement['artifacts']], ['implementation_handoff'])
        self.assertEqual((decisions['VERIFY']['artifacts'], decisions['VERIFY']['historical']), ([], [ident(old, 'verifier_evidence')]))
        self.assertNotIn(str(self.root), json.dumps(plan))  # Stable identities, never paths.
        # Execution history is truthful: only VERIFY ran (and is accounted); reused stages never "executed".
        self.assertEqual(self.model_calls(), calls)
        events = tuple(read_events(self.w2/'events.jsonl'))
        entered = [e.state.split(':')[2] for e in events if e.kind == 'state_entered' and e.state.startswith('@v2:')]
        self.assertEqual(entered, ['VERIFY'])
        self.assertEqual([e.state.split(':')[2] for e in events if e.kind == 'invocation_started'], ['VERIFY'])
        status = recovery_status(self.w2)
        self.assertEqual(status['continuity'], dict(reused=['REQUEST', 'SOURCE_RESOLUTION', 'SPECIFY', 'IMPLEMENT'],
                                                    rerun=['VERIFY'], invalidated=[]))
        self.assertEqual(self.w1_bytes(), w1)
        self.assertIsNone(continuity_plan(self.work))  # Nothing written into W1.
        # Execution never rewrites the decision; the plan survives acceptance and a restart.
        create_qa_candidate(self.d2, execution.workspace, execution.result, 'feature', self.root/'qa2')
        attach_qa_candidate(self.w2, self.root/'qa2'); accept_v2_candidate(self.w2)
        out = subprocess.run([sys.executable, '-c', 'import json, sys; sys.path.insert(0, %r)\nfrom switchyard.continuity import continuity_plan\n'
                              'print(json.dumps(continuity_plan(%r)))' % (str(ROOT), str(self.w2))], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), plan)
        self.assertEqual(recovery_status(self.w2)['continuity']['rerun'], ['VERIFY'])

    # 17
    def test_accepted_source_gives_the_same_logical_plan(self):
        self.w1_ready(); commit = accept_v2_candidate(self.work).committed_head
        self.adopt()
        self.assertEqual(logical(self.w2_plan()), LOGICAL)
        self.assertEqual(self.w2_plan()['decisions'][3]['implementation']['source'], 'accepted_commit:'+commit)

    # 18
    def test_suspended_source_gives_the_same_logical_plan(self):
        self.w1_ready(candidate=False); suspend_v2_work(self.work)
        self.adopt()
        self.assertEqual(logical(self.w2_plan()), LOGICAL)
        self.assertEqual(self.w2_plan()['decisions'][3]['implementation']['source'], 'shelf:1')

    # 12
    def test_retry_after_crash_reproduces_the_identical_plan(self):
        self.w1_ready()
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.adopt()
        written = continuity_plan(self.w2)
        self.adopt()
        self.assertEqual(self.w2_plan(), written)
        store = WorkWorkspace.open(self.w2/'recovery')
        phases = [json.loads(store.read_artifact('operation', r.version))['phase'] for r in store.versions('operation')]
        self.assertEqual(phases.count('provisioning'), 1)

    # 13
    def test_incompatible_existing_plan_is_refused_never_overwritten(self):
        self.w1_ready()
        with patch('switchyard.recovery._worktrees', crash_once(recovery._worktrees)), self.assertRaises(Crash):
            self.adopt()
        store = WorkWorkspace.open(self.w2/'recovery'); op = json.loads(store.read_artifact('operation'))
        tampered = json.loads(json.dumps(op['adoption'])); tampered['continuity']['decisions'][-1]['action'] = 'REUSED'
        _operation(store, 'provisioning', isolation=op['isolation'], adoption=tampered)
        before = (self.w2/'recovery/manifest.jsonl').read_bytes()
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'continuity_plan_differs: retry with the original configuration')
        self.assertEqual((self.w2/'recovery/manifest.jsonl').read_bytes(), before)
        self.assertFalse(self.d2.exists())

    # 14
    def test_artifact_contract_change_is_invalidated_deterministically_and_blocks(self):
        self.w1_ready(); w1 = self.w1_bytes(); calls = self.model_calls()
        cases = (('implementation_handoff', dict(format='text'), 'IMPLEMENT',
                  [('SPECIFY', 'REUSED', 'durable_product_available'), ('IMPLEMENT', 'INVALIDATED', 'artifact_contract_changed')]),
                 ('microspec', dict(format='json', schema='microspec'), 'SPECIFY',
                  [('SPECIFY', 'INVALIDATED', 'artifact_contract_changed'), ('IMPLEMENT', 'INVALIDATED', 'upstream_invalidated: SPECIFY')]))
        for artifact, contract, stage, expected in cases:
            with self.subTest(artifact):
                data = json.loads(self.text); states = data['flows']['feature']['states']
                owner = next(s for s in states.values() if artifact in s.get('produces', {}))
                owner['produces'][artifact] = contract
                plans = []
                for _ in range(2):  # Same durable inputs -> same decisions.
                    with self.assertRaises(RecoveryBlocked) as ctx:
                        self.adopt(text=json.dumps(data))
                    self.assertEqual((ctx.exception.terminal, ctx.exception.reason, ctx.exception.paths),
                                     ('ADOPTION_BLOCKED', 'continuity_invalidated: '+stage, (artifact,)))
                    plans.append(ctx.exception.continuity_plan)
                self.assertEqual(plans[0], plans[1])
                self.assertEqual(logical(plans[0])[2:4], expected)
                self.assertEqual(logical(plans[0])[-1], ('VERIFY', 'RERUN', 'current_runtime_verification_required'))
                changed = next(a for d in plans[0]['decisions'] for a in d['artifacts'] if a['artifact'] == artifact)
                self.assertEqual(changed['current_contract'], dict(format=contract['format'], schema=contract.get('schema'), source=None))
                self.assertFalse((self.w2/'recovery').exists())
        self.assertEqual((self.w1_bytes(), self.model_calls()), (w1, calls))

    # 15, 16
    def test_missing_implementation_and_changed_base_never_produce_a_plan(self):
        self.w1_ready(candidate=False)
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'no_reusable_implementation')
        self.assertIsNone(getattr(ctx.exception, 'continuity_plan', None))
        create_qa_candidate(self.delivery, WorkWorkspace.open(self.work), recovery.RunResult('READY_FOR_MANUAL_QA', tuple(read_events(self.work/'events.jsonl'))),
                            'feature', self.root/'qa1')
        attach_qa_candidate(self.work, self.root/'qa1')
        self.git('checkout', '-q', '--orphan', 'unrelated'); self.git('commit', '-qm', 'unrelated root')
        with self.assertRaises(RecoveryBlocked) as ctx:
            self.adopt()
        self.assertEqual(ctx.exception.reason, 'incompatible_base_lineage')
        self.assertIsNone(getattr(ctx.exception, 'continuity_plan', None))
        self.assertFalse((self.w2/'recovery').exists())

    # 20
    def test_ordinary_delivery_has_no_continuity_plan(self):
        self.w1_ready()
        self.assertIsNone(continuity_plan(self.work))
        self.assertNotIn('continuity', recovery_status(self.work)); self.assertNotIn('adopted', recovery_status(self.work))


if __name__ == '__main__':
    unittest.main()
