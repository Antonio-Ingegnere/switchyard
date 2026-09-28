"""V2 plans execute only through runtime with deterministic fake executors."""
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml
from flow_config import FlowConfigError, _yaml_data
from runtime import ExecutionError, ExecutorResult, InvocationResponse, WorkUnit, run
from runtime.recording import JsonlRecorder
from switchyard.observability import read_events, summarize_runs
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_compiler import compile_workflow, WorkflowCompileError

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/delivery/workflows-v2.yaml'


class FakeResolver:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.bindings, self.executed, self.instances, self.requests = [], [], [], []

    def __call__(self, binding):
        self.bindings.append(binding)
        key = (binding.logical_state, binding.rung_index)
        values = list(self.outcomes.get(key, self.outcomes.get(binding.logical_state,
                      ['pass' if binding.logical_state.startswith('VERIFY') else 'ready'])))
        owner = self
        class Fake:
            def run(self, request):
                owner.executed.append(key)
                owner.requests.append(request)
                outcome = values.pop(0) if len(values) > 1 else values[0]
                return InvocationResponse(ExecutorResult(outcome))
        fake = Fake()
        self.instances.append(fake)
        return fake


def fixture():
    return {
        'version': 2,
        'profiles': {'small': {'provider': 'claude', 'model': 'haiku'},
                     'large': {'provider': 'claude', 'model': 'sonnet'},
                     'other': {'provider': 'codex', 'model': 'codex'},
                     'cmd': {'provider': 'command'}},
        'roles': {'worker': {'primary': {'profile': 'small', 'effort': 'medium'},
                            'escalation': [
                                {'kind': 'vertical', 'profile': 'large', 'effort': 'high',
                                 'context': 'continue', 'on': ['inconclusive', 'budget_exceeded'],
                                 'budget': {'max_invocations': 2}},
                                {'kind': 'horizontal', 'profile': 'other', 'effort': 'high',
                                 'context': 'fresh', 'on': ['diagnosis_disproved']}]},
                  'command': {'primary': {'profile': 'cmd', 'budget': {'max_invocations': 4}}}},
        'flows': {'test': {'entry': 'A', 'run_budget': {'max_invocations': 10, 'on_exceeded': 'STOP'},
                          'states': {'A': {'role': 'worker', 'on': {'ready': 'DONE'},
                                           'budget': {'max_invocations': 10, 'max_state_visits': 2, 'on_exceeded': 'STOP'},
                                           'escalation_exhausted': 'STOP',
                                           'consumes': ['request'], 'produces': ['evidence']},
                                     'DONE': {'terminal': True}, 'STOP': {'terminal': True}}}}}


def loop_fixture():
    data = fixture()
    states = data['flows']['test']['states']
    states['A']['on'] = {'ready': 'RETURN', 'finish': 'DONE'}
    states['RETURN'] = {'role': 'command', 'on': {'ready': 'A'},
                        'budget': {'max_state_visits': 3, 'on_exceeded': 'STOP'}}
    return data


class WorkflowCompilerTests(unittest.TestCase):
    def load(self, data):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'workflow.yaml'
            path.write_text(yaml.safe_dump(data))
            return load_workflow_bundle(path)

    def execute(self, data=None, name='test', outcomes=None):
        bundle = self.load(data or fixture())
        resolver = FakeResolver(outcomes)
        compiled = compile_workflow(bundle, name, resolver)
        result = run(WorkUnit('original request'), compiled.flow, compiled.executors)
        self.assertEqual(len(resolver.bindings), len(compiled.executors))
        self.assertEqual(len(resolver.bindings), len({id(x) for x in resolver.instances}))
        return compiled, resolver, result

    def test_minimal_single_rung_and_terminal_only(self):
        data = fixture(); data['flows']['test']['states']['A'] = {'role': 'command', 'on': {'ready': 'DONE'}}
        compiled, resolver, result = self.execute(data)
        self.assertEqual(result.terminal_state, 'DONE')
        self.assertEqual(resolver.executed, [('A', 0)])
        binding, = resolver.bindings
        self.assertEqual((binding.provider, binding.model, binding.effort, binding.context_mode, binding.escalation_kind),
                         ('command', None, None, None, None))
        states = data['flows']['test']['states']; del states['A']
        data['flows']['test']['entry'] = 'DONE'
        compiled, resolver, result = self.execute(data)
        self.assertEqual(resolver.bindings, [])
        self.assertEqual(result.terminal_state, 'DONE')

    def test_deterministic_immutable_plan_and_artifacts(self):
        data = fixture(); first = compile_workflow(self.load(data), 'test', FakeResolver())
        data['flows']['test']['states'] = dict(reversed(list(data['flows']['test']['states'].items())))
        second = compile_workflow(self.load(data), 'test', FakeResolver())
        self.assertEqual(first.flow, second.flow)
        self.assertEqual(first.plan, second.plan)
        self.assertEqual(first.entry_states, second.entry_states)
        self.assertEqual(tuple(first.executors), tuple(second.executors))
        entry = first.entry_states['A']; info = first.plan[entry]
        self.assertEqual(info.declaration.consumes, ('request',))
        self.assertEqual(info.declaration.produces, ('evidence',))
        self.assertEqual(info.binding.rung_index, 0)
        self.assertIsNone(info.binding.context_mode)
        self.assertEqual(info.state, first.flow.states[entry])
        self.assertEqual(first.entry_states['DONE'], 'DONE')
        self.assertIsNone(first.plan['DONE'].binding)
        for mapping in (first.plan, first.executors, first.entry_states):
            with self.assertRaises(TypeError): mapping['new'] = None
        with self.assertRaises(FrozenInstanceError): info.binding.effort = 'low'
        with self.assertRaises(FrozenInstanceError): first.flow = None
        with self.assertRaises(TypeError): info.state.on['ready'] = 'STOP'

    def test_flow_selection_and_binding_fields(self):
        bundle = load_workflow_bundle(EXAMPLE)
        for name in ('feature', 'bugfix'):
            resolver = FakeResolver(); compiled = compile_workflow(bundle, name, resolver)
            self.assertTrue(all(b.flow_name == name for b in resolver.bindings))
            self.assertEqual(compiled.flow.initial, compiled.entry_states[bundle.flow(name).entry])
        with self.assertRaises(FlowConfigError): compile_workflow(bundle, 'absent', FakeResolver())
        _, resolver, _ = self.execute()
        primary, vertical, horizontal = resolver.bindings
        self.assertEqual((primary.profile_name, primary.provider, primary.model, primary.effort),
                         ('small', 'claude', 'haiku', 'medium'))
        self.assertEqual((vertical.provider, vertical.model, vertical.effort, vertical.context_mode, vertical.escalation_kind),
                         ('claude', 'sonnet', 'high', 'continue', 'vertical'))
        self.assertEqual((horizontal.provider, horizontal.effort, horizontal.context_mode, horizontal.escalation_kind),
                         ('codex', 'high', 'fresh', 'horizontal'))

    def test_resolver_failure_and_invalid_result(self):
        def broken(binding): raise LookupError('adapter unavailable')
        with self.assertRaisesRegex(WorkflowCompileError, 'cannot resolve test.A rung 0') as caught:
            compile_workflow(self.load(fixture()), 'test', broken)
        self.assertIsInstance(caught.exception.__cause__, LookupError)
        with self.assertRaisesRegex(WorkflowCompileError, 'invalid executor'):
            compile_workflow(self.load(fixture()), 'test', lambda binding: object())

    def test_generated_collisions_are_explicit(self):
        for helper in ('_state_id', '_scope_id'):
            with self.subTest(helper=helper), patch('switchyard.workflow_compiler.' + helper, return_value='DONE'):
                with self.assertRaisesRegex(WorkflowCompileError, 'collision'):
                    compile_workflow(self.load(fixture()), 'test', FakeResolver())

    def test_runtime_representation_error_does_not_invent_caps(self):
        data = fixture(); state = data['flows']['test']['states']['A']
        state['on']['repeat'] = 'A'; state['budget'].pop('max_state_visits')
        with self.assertRaisesRegex(WorkflowCompileError, 'cannot represent.*cycle'):
            compile_workflow(self.load(data), 'test', FakeResolver())

    def test_adjacent_only_normal_precedence_and_exhaustion(self):
        data = fixture()
        # Primary may not skip vertical to horizontal on diagnosis_disproved.
        _, resolver, result = self.execute(data, outcomes={'A': ['diagnosis_disproved']})
        self.assertEqual(resolver.executed, [('A', 0)])
        self.assertEqual(result.terminal_state, 'STOP')
        data['flows']['test']['states']['A']['on']['diagnosis_disproved'] = 'DONE'
        _, resolver, result = self.execute(data, outcomes={'A': ['diagnosis_disproved']})
        self.assertEqual(result.terminal_state, 'DONE')
        self.assertEqual(resolver.executed, [('A', 0)])
        data['flows']['test']['states']['A']['on']['inconclusive'] = 'DONE'
        _, resolver, result = self.execute(data, outcomes={('A', 0): ['inconclusive'], ('A', 1): ['diagnosis_disproved'],
                                                          ('A', 2): ['verification_same_failure']})
        self.assertEqual(resolver.executed, [('A', 0), ('A', 1), ('A', 2)])
        self.assertEqual(result.terminal_state, 'STOP')

    def test_unknown_outcome_and_budget_spoof_rejected(self):
        for outcome in ('banana', 'budget_exceeded'):
            with self.subTest(outcome=outcome), self.assertRaises(ExecutionError):
                self.execute(outcomes={'A': [outcome]})

    def test_compiled_two_phase_entry_and_real_rung_refusal(self):
        compiled, resolver, result = self.execute(loop_fixture())
        self.assertEqual(resolver.executed, [('A', 0), ('RETURN', 0), ('A', 1), ('RETURN', 0)])
        self.assertEqual(result.terminal_state, 'STOP')
        entry = compiled.entry_states['A']; summary, = summarize_runs(result.events)
        self.assertEqual(summary.state_visit_counts[entry], 2)
        budgets = [e for e in result.events if e.kind == 'budget_exceeded']
        self.assertEqual(budgets[0].metric, 'invocation_scope:' + compiled.flow.states[entry].invocation_budgets[-1].name)
        self.assertIsNotNone(budgets[0].state_visit_id)
        self.assertEqual(budgets[-1].metric, 'max_state_visits_per_run')
        self.assertIsNone(budgets[-1].state_visit_id)
        for info in compiled.plan.values():
            if info.binding and info.binding.logical_state == 'A' and info.binding.rung_index:
                self.assertIsNone(info.state.max_state_visits_per_run)

    def test_state_scope_wins_when_both_scopes_exhausted(self):
        data = loop_fixture(); data['flows']['test']['states']['A']['budget']['max_invocations'] = 1
        compiled, resolver, result = self.execute(data)
        self.assertEqual(resolver.executed, [('A', 0), ('RETURN', 0)])
        scopes = compiled.flow.states[compiled.entry_states['A']].invocation_budgets
        self.assertEqual([s.limit for s in scopes], [1, 1])
        self.assertEqual(scopes[0].on_exceeded, 'STOP')
        self.assertNotEqual(scopes[1].on_exceeded, 'STOP')
        budget, = (e for e in result.events if e.kind == 'budget_exceeded')
        self.assertEqual(budget.metric, 'invocation_scope:' + scopes[0].name)
        self.assertEqual(budget.target, 'STOP')

    def test_shared_scope_counts_all_rungs(self):
        data = fixture(); data['flows']['test']['states']['A']['budget']['max_invocations'] = 2
        compiled, resolver, result = self.execute(data, outcomes={('A', 0): ['inconclusive'], ('A', 1): ['diagnosis_disproved']})
        self.assertEqual(resolver.executed, [('A', 0), ('A', 1)])
        scopes = [info.state.invocation_budgets[0] for info in compiled.plan.values() if info.binding]
        self.assertEqual(len(set(scopes)), 1)
        budget, = (e for e in result.events if e.kind == 'budget_exceeded')
        self.assertEqual((budget.observed, budget.limit, budget.target), (2, 2, 'STOP'))

    def test_rung_budget_normal_route_before_exhaustion(self):
        data = loop_fixture()
        data['roles']['worker']['escalation'][0]['on'] = ['inconclusive']
        data['flows']['test']['states']['A']['on']['budget_exceeded'] = 'DONE'
        _, resolver, result = self.execute(data)
        self.assertEqual(result.terminal_state, 'DONE')
        self.assertEqual(resolver.executed, [('A', 0), ('RETURN', 0)])
        del data['flows']['test']['states']['A']['on']['budget_exceeded']
        _, _, result = self.execute(data)
        self.assertEqual(result.terminal_state, 'STOP')

    def test_unhandled_nonladder_refusal_is_runtime_error(self):
        data = loop_fixture(); data['roles']['worker'].pop('escalation')
        data['flows']['test']['states']['A'].pop('escalation_exhausted')
        with self.assertRaisesRegex(ExecutionError, 'missing transition'):
            self.execute(data)

    def test_canonical_feature_paths(self):
        data = _yaml_data(EXAMPLE.read_text())
        for outcomes, expected, terminal in [
            ({}, [('SPECIFY', 0), ('IMPLEMENT', 0), ('VERIFY', 0)], 'READY_FOR_MANUAL_QA'),
            ({'VERIFY': ['fail'], ('DIAGNOSE_FAILURE', 0): ['inconclusive']},
             [('SPECIFY', 0), ('IMPLEMENT', 0), ('VERIFY', 0), ('DIAGNOSE_FAILURE', 0),
              ('DIAGNOSE_FAILURE', 1), ('REPAIR_ONCE', 0), ('VERIFY_AGAIN', 0)], 'READY_FOR_MANUAL_QA'),
            ({'VERIFY': ['fail'], 'DIAGNOSE_FAILURE': ['inconclusive']},
             [('SPECIFY', 0), ('IMPLEMENT', 0), ('VERIFY', 0), ('DIAGNOSE_FAILURE', 0),
              ('DIAGNOSE_FAILURE', 1), ('DIAGNOSE_FAILURE', 2)], 'NEEDS_REDIAGNOSIS')]:
            with self.subTest(terminal=terminal, outcomes=outcomes):
                compiled, resolver, result = self.execute(data, 'feature', outcomes)
                self.assertEqual(resolver.executed, expected)
                self.assertEqual(result.terminal_state, terminal)
                self.assertEqual(len(resolver.bindings), 8)
                for request in resolver.requests:
                    self.assertEqual(request.work_unit.task, 'original request')
                    self.assertIsNone(request.diagnostics)  # No fabricated artifact/context transport.
                first = compiled.plan[compiled.entry_states['VERIFY']]
                second = compiled.plan[compiled.entry_states['VERIFY_AGAIN']]
                self.assertEqual(first.binding.role_name, second.binding.role_name)
                self.assertNotEqual(first.state.invocation_budgets, second.state.invocation_budgets)

    def test_canonical_feature_state_and_run_refusal(self):
        data = _yaml_data(EXAMPLE.read_text())
        data['flows']['feature']['states']['DIAGNOSE_FAILURE']['budget']['max_invocations'] = 1
        _, resolver, result = self.execute(data, 'feature', {'VERIFY': ['fail'], ('DIAGNOSE_FAILURE', 0): ['inconclusive']})
        self.assertEqual(result.terminal_state, 'NEEDS_HUMAN')
        self.assertNotIn(('DIAGNOSE_FAILURE', 1), resolver.executed)
        data['flows']['feature']['run_budget']['max_invocations'] = 1
        _, resolver, result = self.execute(data, 'feature')
        self.assertEqual(resolver.executed, [('SPECIFY', 0)])
        self.assertEqual(result.terminal_state, 'NEEDS_HUMAN')
        self.assertEqual(next(e.metric for e in result.events if e.kind == 'budget_exceeded'), 'max_invocations_per_run')

    def test_canonical_bugfix_paths_and_specific_rediagnosis_role(self):
        data = _yaml_data(EXAMPLE.read_text())
        for outcomes, diagnosis, tail in [
            ({}, [('DIAGNOSE', 0)], [('VERIFY', 0)]),
            ({('DIAGNOSE', 0): ['inconclusive']}, [('DIAGNOSE', 0), ('DIAGNOSE', 1)], [('VERIFY', 0)]),
            ({('DIAGNOSE', 0): ['inconclusive'], ('DIAGNOSE', 1): ['diagnosis_disproved']},
             [('DIAGNOSE', 0), ('DIAGNOSE', 1), ('DIAGNOSE', 2)], [('VERIFY', 0)]),
            ({'VERIFY': ['fail']}, [('DIAGNOSE', 0)], [('VERIFY', 0), ('REDIAGNOSE', 0), ('REFIX', 0), ('VERIFY_AGAIN', 0)])]:
            with self.subTest(outcomes=outcomes):
                compiled, resolver, result = self.execute(data, 'bugfix', outcomes)
                self.assertEqual(resolver.executed, [('TRIAGE', 0), ('REPRODUCE', 0), *diagnosis,
                                                     ('SPECIFY_FIX', 0), ('FIX', 0), *tail])
                self.assertEqual(result.terminal_state, 'READY_FOR_MANUAL_QA')
                self.assertEqual(len(resolver.bindings), 11)
                binding = compiled.plan[compiled.entry_states['REDIAGNOSE']].binding
                self.assertEqual((binding.role_name, binding.profile_name, binding.provider), ('rediagnostician', 'codex', 'codex'))

    def test_bugfix_real_rung_refusal_and_enclosing_state_precedence(self):
        for state_cap, expected_terminal, used_alternate in [(3, 'READY_FOR_MANUAL_QA', True), (1, 'NEEDS_HUMAN', False)]:
            with self.subTest(state_cap=state_cap):
                data = _yaml_data(EXAMPLE.read_text())
                states = data['flows']['bugfix']['states']
                states['DIAGNOSE']['budget'].update(max_state_visits=2, max_invocations=state_cap)
                states['SPECIFY_FIX']['on']['recheck'] = 'DIAGNOSE'
                states['SPECIFY_FIX']['budget'].update(max_state_visits=2, max_invocations=2)
                data['roles']['specifier']['primary']['budget'] = {'max_invocations': 2}
                compiled, resolver, result = self.execute(data, 'bugfix', {'SPECIFY_FIX': ['recheck', 'ready']})
                self.assertEqual(result.terminal_state, expected_terminal)
                self.assertEqual(('DIAGNOSE', 1) in resolver.executed, used_alternate)
                self.assertEqual(resolver.executed.count(('DIAGNOSE', 0)), 1)
                entry = compiled.entry_states['DIAGNOSE']
                scopes = compiled.flow.states[entry].invocation_budgets
                budget = next(e for e in result.events if e.kind == 'budget_exceeded')
                self.assertEqual(budget.metric, 'invocation_scope:' + scopes[1 if used_alternate else 0].name)
                self.assertEqual(summarize_runs(result.events)[0].state_visit_counts[entry], 2)

    def test_low_effort_and_semantic_values_are_not_translated(self):
        data = fixture(); data['roles']['worker']['primary']['effort'] = 'low'
        data['flows']['test']['states']['A']['on'] = {'reproduced': 'DONE', 'not_reproduced': 'STOP'}
        for outcome, terminal in [('reproduced', 'DONE'), ('not_reproduced', 'STOP')]:
            compiled, resolver, result = self.execute(data, outcomes={'A': [outcome]})
            self.assertEqual(resolver.bindings[0].effort, 'low')
            self.assertEqual(result.terminal_state, terminal)
            self.assertEqual(next(e.outcome for e in result.events if e.kind == 'invocation_finished'), outcome)

    def test_recording_generic_observability_and_timing(self):
        compiled = compile_workflow(self.load(loop_fixture()), 'test', FakeResolver())
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            with patch('runtime.time.time_ns', return_value=100), patch('runtime.time.monotonic_ns', side_effect=range(1000)):
                result = run(WorkUnit('t'), compiled.flow, compiled.executors, recorder=JsonlRecorder(path))
            events = tuple(read_events(path))
        self.assertEqual(events, result.events)
        summary, = summarize_runs(events)
        self.assertEqual(summary.invocation_count, 4)
        self.assertEqual(summary.budget_exceeded_count, 2)
        self.assertEqual(summary.state_visit_counts[compiled.entry_states['A']], 2)
        self.assertEqual(summary.invocation_duration_ns_total, 4)
        self.assertEqual(summary.run_duration_ns, len(events)-1)
