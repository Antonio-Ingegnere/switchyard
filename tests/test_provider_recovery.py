"""PROVIDER-RECOVERY-001: infrastructure failures are not semantic invocations.

Real processes and real Git through the RecoveryTests fixture; no live providers.
"""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from runtime import ExecutionError, ExecutorResult, Flow, InvocationBudgetScope, InvocationResponse, State, WorkUnit, run
from flow_config import FlowConfigError
from switchyard import recovery as recovery_module
from switchyard.provider_process import ProviderExecutionError
from switchyard.recovery import (RecoveryBlocked, _Repo, recovery_status, resume_v2_work, rollback_v2_work)
from switchyard.simple_config import simple_config_from_text
from switchyard.work_contract import WorkWorkspace
from switchyard.observability import read_events, summarize_runs

import test_recovery  # Module import: do not re-collect RecoveryTests here.

# Exact key set of an interruption record written by Switchyard 2.2.0 (real Adeo work 462efa...).
LEGACY_KEYS = {'diagnostic','manifest_before','phase','reason','resume_safe','returncode','rung','state','timestamp'}


def _digest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob('*')) if p.is_file() and p.name != '.recovery.lock'}


class ProviderRecoveryTests(unittest.TestCase):
    _base = test_recovery.RecoveryTests
    setUp, git, save_config = _base.setUp, _base.git, _base.save_config
    start, interrupt, resume = _base.start, _base.interrupt, _base.resume

    def provider(self, old, new):
        exe = self.root/'claude'; text = exe.read_text()
        self.assertIn(old, text); exe.write_text(text.replace(old, new, 1))

    def fail_once(self):
        """IMPLEMENT edits a file, then the provider exits 1 before any StageResult."""
        self.provider('time.sleep(3)', 'raise SystemExit(1)')

    def rung_budget(self, limit):
        self.data['flows']['feature']['states']['IMPLEMENT']['run'][0]['budget']['invocations'] = limit
        self.text = self.save_config()

    def calls(self):
        return [json.loads(l)['binding']['logical_state'] for l in self.log.read_text().splitlines()]

    def operations(self):
        store = WorkWorkspace.open(self.work/'recovery')
        return [json.loads(store.read_artifact('operation', r.version)) for r in store.versions('operation')]

    def events(self):
        return tuple(read_events(self.work/'events.jsonl'))

    # Test 1
    def test_provider_exit_before_result_does_not_consume_semantic_invocation(self):
        self.rung_budget(1); self.fail_once(); self.interrupt()
        status = recovery_status(self.work)
        self.assertEqual((status['terminal'], status['state'], status['reason']), ('INTERRUPTED', 'IMPLEMENT', 'provider_failure'))
        self.assertEqual((status['semantic_invocations_used'], status['semantic_invocations_limit']), (0, 1))
        self.assertEqual((status['infrastructure_failures_used'], status['infrastructure_failures_limit']), (1, 2))
        self.assertIs(status['semantic_invocation_charged'], False)
        self.assertEqual((status['resume_allowed'], status['resume_to']), (True, 'IMPLEMENT'))
        failure = status['provider_failure']
        self.assertEqual({k: failure[k] for k in ('classification','provider','model','agent','exit_code','timed_out')},
                         dict(classification='provider_exit', provider='claude', model='sonnet', agent='code',
                              exit_code=1, timed_out=False))
        self.assertIsInstance(failure['duration_ms'], int)
        self.assertEqual(status['diagnostic'], 'provider exited with status 1')  # Raw output stays out of diagnostic.
        op = self.operations()[-1]
        self.assertEqual((op['semantic_invocation'], op['invocation_id']), (False, self.events()[-1].invocation_id))

    # Test 2
    def test_resume_after_provider_failure_continues_same_work_and_charges_once(self):
        self.rung_budget(1); self.fail_once(); self.interrupt()
        before = self.events(); spec = WorkWorkspace.open(self.work).read_artifact('microspec')
        execution = self.resume()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        events = self.events()
        self.assertEqual(events[:len(before)], before)
        self.assertEqual(len({e.run_id for e in events}), 1)
        implement = [e for e in events if e.kind == 'invocation_started' and 'IMPLEMENT' in e.state]
        self.assertEqual(len(implement), 2); self.assertEqual(len({e.invocation_id for e in events if e.invocation_id}), 4)
        finished = [e for e in events if e.kind == 'invocation_finished' and 'IMPLEMENT' in e.state]
        self.assertEqual([e.invocation_id for e in finished], [implement[1].invocation_id])
        self.assertEqual(len([e for e in events if e.kind == 'state_entered' and 'IMPLEMENT' in e.state]), 1)
        self.assertEqual(self.calls(), ['SPECIFY', 'IMPLEMENT', 'IMPLEMENT'])  # SPECIFY never reruns.
        workspace = WorkWorkspace.open(self.work)
        self.assertEqual((len(workspace.versions('microspec')), workspace.read_artifact('microspec')), (1, spec))
        self.assertEqual(self.file.read_text(), 'finished')
        self.assertTrue(summarize_runs(events)[0].completed_normally)

    # Test 3
    def test_repeated_provider_failures_exhaust_bounded_infrastructure_allowance(self):
        self.rung_budget(1); self.provider("if state=='IMPLEMENT':\n", "if state=='IMPLEMENT':\n    raise SystemExit(1)\n")
        self.interrupt()
        with self.assertRaises(ExecutionError): self.resume()
        status = recovery_status(self.work)
        self.assertEqual((status['infrastructure_failures_used'], status['semantic_invocations_used']), (2, 0))
        self.assertEqual((status['resume_allowed'], status['resume_to'], status['resume_blocked']),
                         (False, None, 'infrastructure_budget_exhausted'))
        calls = self.log.read_bytes()
        with self.assertRaisesRegex(RecoveryBlocked, 'RESUME_BLOCKED: infrastructure_budget_exhausted'): self.resume()
        self.assertEqual(self.log.read_bytes(), calls)  # Third provider process refused.
        self.assertTrue(recovery_status(self.work)['rollback_allowed'])

    def test_infrastructure_failure_allowance_is_configurable_and_frozen(self):
        self.data['agents']['code']['infrastructure_failures'] = 1
        self.rung_budget(1); self.fail_once(); self.interrupt()
        status = recovery_status(self.work)
        self.assertEqual((status['resume_allowed'], status['resume_blocked']), (False, 'infrastructure_budget_exhausted'))
        self.data['agents']['code']['infrastructure_failures'] = 3
        with self.assertRaisesRegex(RecoveryBlocked, 'semantic_configuration_changed'): self.resume()

    # Test 4
    def test_valid_negative_stage_result_consumes_semantic_invocation(self):
        implement = self.data['flows']['feature']['states']['IMPLEMENT']
        implement['on']['blocked'] = 'NEEDS_HUMAN'; implement['budget'] = {'visits': 2}
        self.data['flows']['feature']['states']['NEEDS_HUMAN'] = {'terminal': True, 'resume_to': 'IMPLEMENT'}
        self.rung_budget(1)
        self.provider('time.sleep(3)', '')
        self.provider('raw=json.dumps(result)', "if state=='IMPLEMENT': result['outcome']='blocked'\nraw=json.dumps(result)")
        result = self.start()
        self.assertEqual(result.result.terminal_state, 'NEEDS_HUMAN')
        self.assertIn(('invocation_finished', 'blocked'), [(e.kind, e.outcome) for e in self.events() if e.state and 'IMPLEMENT' in e.state])
        status = recovery_status(self.work)
        self.assertEqual((status['semantic_invocations_used'], status['semantic_invocations_limit']), (1, 1))
        self.assertEqual((status['resume_allowed'], status['resume_blocked']), (False, 'invocation_budget_exhausted'))
        with self.assertRaisesRegex(RecoveryBlocked, 'invocation_budget_exhausted'):
            resume_v2_work(self.work, self.text, self.config)

    def test_failure_after_stage_result_is_charged_even_if_provider_error_type(self):
        """Boundary: once the adapter returns a StageResult, later failures never void it."""
        self.rung_budget(1); self.provider('time.sleep(3)', '')
        original = WorkWorkspace.write_artifact
        def write(store, name, *args, **kwargs):
            if name == 'implementation_handoff': raise ProviderExecutionError('late persistence failure')
            return original(store, name, *args, **kwargs)
        with patch.object(WorkWorkspace, 'write_artifact', write): self.interrupt()
        op = self.operations()[-1]
        self.assertEqual((op['reason'], op['semantic_invocation']), ('provider_failure', True))
        self.assertNotIn('provider_failure', op)
        status = recovery_status(self.work)
        self.assertEqual((status['semantic_invocations_used'], status['semantic_invocation_charged']), (1, True))
        self.assertFalse(status['resume_allowed'])
        with self.assertRaisesRegex(RecoveryBlocked, 'invocation_budget_exhausted'): self.resume()

    # Test 5
    def test_timeout_before_result_checkpoints_partial_edit_without_semantic_charge(self):
        self.rung_budget(1); self.interrupt()
        import os
        with self.assertRaises(ProcessLookupError): os.kill(int((self.root/'pid').read_text()), 0)
        status = recovery_status(self.work)
        self.assertEqual((status['reason'], status['provider_failure']['classification'], status['provider_failure']['timed_out']),
                         ('provider_timeout', 'timeout', True))
        self.assertEqual((status['semantic_invocations_used'], status['infrastructure_failures_used']), (0, 1))
        self.assertTrue(status['resume_allowed'])
        store = WorkWorkspace.open(self.work/'recovery')
        checkpoint = json.loads(store.read_artifact('checkpoint'))['snapshot']['files']
        self.assertEqual(checkpoint['source.py']['sha256'], hashlib.sha256(b'partial').hexdigest())
        self.assertNotIn('remove.txt', checkpoint)
        self.assertEqual(self.resume().result.terminal_state, 'READY_FOR_MANUAL_QA')  # Partial edit survives resume.

    # Test 6
    def test_switchyard_2_2_history_becomes_resumable_without_rewrite(self):
        self.rung_budget(1); self.fail_once()
        writer = recovery_module._operation
        def legacy(store, phase, **fields):
            if phase == 'interrupted':
                for key in ('invocation_id', 'semantic_invocation', 'provider_failure'): fields.pop(key, None)
            return writer(store, phase, **fields)
        with patch.object(recovery_module, '_operation', legacy): self.interrupt()
        ops = self.operations()
        self.assertEqual(set(ops[-1]), LEGACY_KEYS)
        self.assertEqual((ops[-1]['reason'], ops[-1]['resume_safe']), ('provider_failure', True))
        history = self.events()
        # 2.2 accounting: every invocation_started is charged, so IMPLEMENT (limit 1) was exhausted.
        self.assertEqual(sum(e.kind == 'invocation_started' and 'IMPLEMENT' in e.state for e in history), 1)
        snapshot = _digest(self.work)
        status = recovery_status(self.work)
        self.assertEqual(_digest(self.work), snapshot)  # Reading never edits history.
        self.assertEqual((status['resume_allowed'], status['resume_to'], status['semantic_invocations_used'],
                          status['infrastructure_failures_used']), (True, 'IMPLEMENT', 0, 1))
        self.assertNotIn('provider_failure', status)  # 2.2 recorded none; nothing is invented.
        execution = self.resume()
        self.assertEqual(execution.result.terminal_state, 'READY_FOR_MANUAL_QA')
        after = self.operations()
        self.assertEqual(after[:len(ops)], ops)  # Append-only.
        inferred = [op['inferred_interruption'] for op in after[len(ops):] if 'inferred_interruption' in op]
        self.assertEqual(inferred, [dict(invocation_id=history[-1].invocation_id, semantic_invocation=False,
                                         basis='switchyard<=2.2 provider interruption without StageResult')])
        self.assertEqual(self.events()[:len(history)], history)
        # The persisted inference keeps later accounting stable after the legacy record is superseded.
        self.assertEqual(recovery_status(self.work)['phase'], 'delivery_done')

    def test_legacy_execution_interruption_stays_charged(self):
        self.rung_budget(1); self.fail_once()
        writer = recovery_module._operation
        def legacy(store, phase, **fields):
            if phase == 'interrupted':
                for key in ('invocation_id', 'semantic_invocation', 'provider_failure'): fields.pop(key, None)
                fields.update(reason='execution_interrupted', resume_safe=False)
            return writer(store, phase, **fields)
        with patch.object(recovery_module, '_operation', legacy): self.interrupt()
        self.assertFalse(recovery_status(self.work)['resume_allowed'])
        with self.assertRaisesRegex(RecoveryBlocked, 'not_a_resumable_interruption'): self.resume()

    def test_resumed_failure_then_second_failure_keeps_both_void(self):
        self.rung_budget(1)
        self.provider("if state=='IMPLEMENT':\n", "if state=='IMPLEMENT':\n    raise SystemExit(1)\n")
        self.data['agents']['code']['infrastructure_failures'] = 3; self.text = self.save_config()
        self.interrupt()
        with self.assertRaises(ExecutionError): self.resume()
        status = recovery_status(self.work)
        self.assertEqual((status['semantic_invocations_used'], status['infrastructure_failures_used'], status['resume_allowed']),
                         (0, 2, True))

    def test_infrastructure_failures_config_validation_and_show(self):
        for value in (0, -1, 1.5, True, 'two'):
            with self.subTest(value=value):
                self.data['agents']['code']['infrastructure_failures'] = value
                with self.assertRaisesRegex(FlowConfigError, 'agents.code.infrastructure_failures'):
                    simple_config_from_text(json.dumps(self.data))
        self.data['agents']['code']['infrastructure_failures'] = 4
        rows = {r['agent']: r for r in simple_config_from_text(json.dumps(self.data)).show()['invocations']}
        self.assertEqual((rows['code']['infrastructure_failures'], rows['spec']['infrastructure_failures']), (4, 2))
        self.data['agents']['tests']['infrastructure_failures'] = 1
        self.assertEqual(simple_config_from_text(json.dumps(self.data)).agents['tests'].infrastructure_failures, 1)


class Raises:
    def run(self, request): raise RuntimeError('provider down')


class Passes:
    def run(self, request): return InvocationResponse(ExecutorResult('pass'))


class CoreVoidInvocationTests(unittest.TestCase):
    def flow(self):
        scope = InvocationBudgetScope('a', 1, 'OUT')
        return Flow('A', {'A': State('a', {'pass': 'DONE'}, invocation_budgets=(scope,)),
                          'DONE': State(terminal=True), 'OUT': State(terminal=True)})

    def interrupted(self):
        with self.assertRaises(ExecutionError) as caught: run(WorkUnit('w'), self.flow(), {'a': Raises()})
        return caught.exception.events

    def test_unvoided_history_is_charged(self):
        result = run(WorkUnit('w'), self.flow(), {'a': Passes()}, resume_from=self.interrupted())
        self.assertEqual(result.terminal_state, 'OUT')

    def test_void_invocation_is_not_charged_and_ids_are_not_reused(self):
        history = self.interrupted()
        result = run(WorkUnit('w'), self.flow(), {'a': Passes()}, resume_from=history,
                     void_invocations=frozenset({history[-1].invocation_id}))
        self.assertEqual(result.terminal_state, 'DONE')
        ids = [e.invocation_id for e in result.events if e.kind == 'invocation_started']
        self.assertEqual(len(ids), len(set(ids)))

    def test_void_requires_unfinished_interrupted_history_invocation(self):
        history = self.interrupted()
        finished = run(WorkUnit('w'), self.flow(), {'a': Passes()}, resume_from=history,
                       void_invocations=frozenset({history[-1].invocation_id})).events
        for resume, void in ((None, {history[-1].invocation_id}), (history, {'run:x:invocation:9'})):
            with self.subTest(void=void), self.assertRaisesRegex(ValueError, 'void invocations'):
                run(WorkUnit('w'), self.flow(), {'a': Passes()}, resume_from=resume, void_invocations=frozenset(void))
        started = next(e.invocation_id for e in finished if e.kind == 'invocation_finished')
        with self.assertRaisesRegex(ValueError, 'resume requires|void invocations'):
            run(WorkUnit('w'), self.flow(), {'a': Passes()}, resume_from=finished, void_invocations=frozenset({started}))


if __name__ == '__main__':
    unittest.main()
