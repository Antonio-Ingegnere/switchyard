"""Budget causes route independently without changing admission or accounting."""
from dataclasses import fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import (Event, ExecutionError, ExecutorResult, Flow, FlowValidationError,
                     InvocationBudgetScope as Scope, InvocationResponse, State, WorkUnit, run)
from runtime.recording import JsonlRecorder
from switchyard.observability import ObservabilityError, read_events, summarize_runs


class Fake:
    def __init__(self, outcome='ready'):
        self.outcome = outcome
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return InvocationResponse(ExecutorResult(self.outcome))


def mixed(visits=1, cap=1, first=1, second=1):
    return Flow('A', {
        'A': State('x', {'ready': 'DONE', 'budget_exceeded': 'LEGACY'},
                   max_state_visits_per_run=visits,
                   invocation_budgets=(Scope('shared', first, 'SHARED'), Scope('local', second, 'NEXT')),
                   state_visit_budget_exceeded='VISITS'),
        'NEXT': State('next', {'ready': 'DONE'}),
        **{name: State(terminal=True) for name in ('DONE', 'LEGACY', 'VISITS', 'SHARED', 'RUN')},
    }, max_invocations_per_run=cap, max_invocations_exceeded='RUN')


class CauseBudgetRoutingTests(unittest.TestCase):
    def test_four_causes_on_one_state_and_first_refusal_precedence(self):
        for limits, metric, target, terminal in [
            ({'visits': 0, 'cap': 0, 'first': 0, 'second': 0}, 'max_state_visits_per_run', 'VISITS', 'VISITS'),
            ({'cap': 0, 'first': 0, 'second': 0}, 'max_invocations_per_run', 'RUN', 'RUN'),
            ({'first': 0, 'second': 0}, 'invocation_scope:shared', 'SHARED', 'SHARED'),
            ({'second': 0}, 'invocation_scope:local', 'NEXT', 'DONE')]:
            with self.subTest(metric=metric):
                x, nxt = Fake(), Fake()
                result = run(WorkUnit('task'), mixed(**limits), {'x': x, 'next': nxt})
                budget, transition = result.events[1:3]
                self.assertEqual((budget.kind, budget.metric, budget.limit, budget.observed, budget.target),
                                 ('budget_exceeded', metric, 0, 0, target))
                self.assertEqual((transition.kind, transition.target, transition.trigger_event),
                                 ('transition_selected', target, 'budget_exceeded'))
                self.assertEqual(result.terminal_state, terminal)
                self.assertEqual(x.calls, 0)
                self.assertEqual(nxt.calls, 1 if target == 'NEXT' else 0)
                self.assertFalse(any(e.state == 'A' and e.kind in ('state_entered', 'invocation_started') for e in result.events))
                for event in (budget, transition):
                    self.assertIsNone(event.state_visit_id)
                    self.assertIsNone(event.invocation_id)
                    self.assertIsNone(event.duration_ns)
                self.assertEqual(summarize_runs(result.events)[0].budget_exceeded_count, 1)

    def test_legacy_fallback_for_each_cause_unchanged(self):
        for visits, cap, scopes in [(0, None, ()), (None, 0, ()), (None, None, (Scope('x', 0),))]:
            with self.subTest(visits=visits, cap=cap):
                flow = Flow('A', {'A': State('x', {'budget_exceeded': 'DONE'},
                    max_state_visits_per_run=visits, invocation_budgets=scopes), 'DONE': State(terminal=True)}, cap)
                fake = Fake()
                result = run(WorkUnit('t'), flow, {'x': fake})
                self.assertEqual([e.kind for e in result.events], ['run_started', 'budget_exceeded',
                    'transition_selected', 'state_entered', 'state_exited', 'run_finished'])
                self.assertIsNone(result.events[1].target)
                self.assertEqual(result.events[2].target, 'DONE')
                self.assertEqual(fake.calls, 0)

    def test_entry_refusal_does_not_execute_local_scope_destination(self):
        for visit_limit, expected, next_calls in [(1, 'STOP', 0), (2, 'DONE', 1)]:
            with self.subTest(visit_limit=visit_limit):
                flow = Flow('A', {
                    'A': State('a', {'ready': 'RETURN'}, max_state_visits_per_run=visit_limit,
                               invocation_budgets=(Scope('shared', 3, 'STOP'), Scope('local', 1, 'NEXT')),
                               state_visit_budget_exceeded='STOP'),
                    'RETURN': State('back', {'ready': 'A'}),
                    'NEXT': State('next', {'ready': 'DONE'}, invocation_budgets=(Scope('shared', 3, 'STOP'),)),
                    'STOP': State(terminal=True), 'DONE': State(terminal=True)}, 10, 'STOP')
                a, back, nxt = Fake(), Fake(), Fake()
                result = run(WorkUnit('t'), flow, {'a': a, 'back': back, 'next': nxt})
                self.assertEqual(result.terminal_state, expected)
                self.assertEqual((a.calls, back.calls, nxt.calls), (1, 1, next_calls))
                budget, = (e for e in result.events if e.kind == 'budget_exceeded')
                self.assertEqual(budget.metric, 'max_state_visits_per_run' if visit_limit == 1 else 'invocation_scope:local')
                self.assertEqual(budget.observed, 1)

    def test_visit_specific_route_can_enter_executable_target(self):
        flow = mixed(visits=0)
        states = dict(flow.states)
        states['A'] = replace(states['A'], state_visit_budget_exceeded='NEXT')
        x, nxt = Fake(), Fake()
        result = run(WorkUnit('t'), replace(flow, states=states), {'x': x, 'next': nxt})
        self.assertEqual((x.calls, nxt.calls), (0, 1))
        self.assertEqual(result.events[1].target, 'NEXT')
        self.assertEqual(result.events[3].state, 'NEXT')
        self.assertTrue(result.events[3].state_visit_id.endswith(':visit:1'))

    def test_shared_allowance_wins_over_local_allowance(self):
        flow = Flow('A0', {
            'A0': State('x', {'ready': 'A1'}, invocation_budgets=(Scope('shared', 1, 'STOP'), Scope('first', 1, 'A1'))),
            'A1': State('y', {'ready': 'DONE'}, invocation_budgets=(Scope('shared', 1, 'STOP'), Scope('second', 0, 'DONE'))),
            'STOP': State(terminal=True), 'DONE': State(terminal=True)}, 5, 'STOP')
        x, y = Fake(), Fake()
        result = run(WorkUnit('t'), flow, {'x': x, 'y': y})
        self.assertEqual((x.calls, y.calls), (1, 0))
        budget, = (e for e in result.events if e.kind == 'budget_exceeded')
        self.assertEqual((budget.metric, budget.target), ('invocation_scope:shared', 'STOP'))

    def test_atomic_refusal_leaves_other_scope_and_run_capacity_available(self):
        flow = Flow('A', {
            'A': State('x', {'ready': 'B'}, invocation_budgets=(Scope('used', 1, 'C'),)),
            'B': State('x', {'ready': 'DONE'}, invocation_budgets=(Scope('spare', 1), Scope('used', 1, 'C'))),
            'C': State('x', {'ready': 'DONE'}, invocation_budgets=(Scope('spare', 1),)),
            'DONE': State(terminal=True)}, 2, 'DONE')
        x = Fake(); result = run(WorkUnit('t'), flow, {'x': x})
        self.assertEqual(x.calls, 2)
        self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'], ['A', 'C'])
        self.assertEqual(summarize_runs(result.events)[0].budget_exceeded_count, 1)

    def test_shared_routes_must_agree_including_absence(self):
        for other in ('Y', None):
            flow = Flow('A', {'A': State('x', invocation_budgets=(Scope('shared', 1, 'X'),)),
                              'B': State('x', invocation_budgets=(Scope('shared', 1, other),)),
                              'X': State(terminal=True), 'Y': State(terminal=True)})
            with self.subTest(other=other), self.assertRaisesRegex(FlowValidationError, 'conflicting'):
                run(WorkUnit('t'), flow, {'x': Fake()})

    def test_invalid_route_targets_and_missing_limits(self):
        for target in ('absent', '', True, 3, [], {}):
            for kind in ('run', 'visit', 'scope'):
                with self.subTest(kind=kind, target=target):
                    kwargs = {'max_state_visits_per_run': 1}
                    if kind == 'visit': kwargs['state_visit_budget_exceeded'] = target
                    if kind == 'scope': kwargs['invocation_budgets'] = (Scope('x', 1, target),)
                    flow = Flow('A', {'A': State('x', **kwargs), 'DONE': State(terminal=True)},
                                1, target if kind == 'run' else None)
                    with self.assertRaises(FlowValidationError): run(WorkUnit('t'), flow, {'x': Fake()})
        for flow in (
            Flow('A', {'A': State('x'), 'DONE': State(terminal=True)}, 1, 'A'),
            Flow('A', {'A': State('x'), 'DONE': State(terminal=True)}, None, 'DONE'),
            Flow('A', {'A': State('x', state_visit_budget_exceeded='DONE'), 'DONE': State(terminal=True)}),
            Flow('DONE', {'DONE': State(terminal=True, state_visit_budget_exceeded='DONE')})):
            with self.subTest(flow=flow), self.assertRaises(FlowValidationError):
                run(WorkUnit('t'), flow, {'x': Fake()})

    def test_specific_refusal_cycles_and_mixed_cycles_rejected(self):
        for states in (
            {'A': State('x', max_state_visits_per_run=0, state_visit_budget_exceeded='B'),
             'B': State('x', max_state_visits_per_run=0, state_visit_budget_exceeded='A')},
            {'A': State('x', invocation_budgets=(Scope('a', 0, 'B'),)),
             'B': State('x', invocation_budgets=(Scope('b', 0, 'A'),))},
            {'A': State('x', max_state_visits_per_run=1, state_visit_budget_exceeded='B'),
             'B': State('x', {'ready': 'A'})}):
            for initial in ('A', 'DONE'):
                with self.subTest(initial=initial, states=states), self.assertRaisesRegex(FlowValidationError, 'cycle'):
                    run(WorkUnit('t'), Flow(initial, {**states, 'DONE': State(terminal=True)}), {'x': Fake()})

    def test_scope_route_is_exact_identity_not_metric_parsing(self):
        name = 'max_invocations_per_run:invocation_scope:x/y'
        flow = Flow('A', {'A': State('x', invocation_budgets=(Scope(name, 0, 'DONE'),)),
                          'DONE': State(terminal=True)})
        result = run(WorkUnit('t'), flow, {'x': Fake()})
        self.assertEqual(result.events[1].metric, 'invocation_scope:' + name)
        self.assertEqual(result.events[1].target, 'DONE')

    def test_executor_cannot_spoof_refusal(self):
        with self.assertRaisesRegex(ExecutionError, 'reserved control-plane outcome') as caught:
            run(WorkUnit('t'), mixed(), {'x': Fake('budget_exceeded'), 'next': Fake()})
        self.assertFalse(any(e.kind == 'budget_exceeded' for e in caught.exception.events))

    def test_jsonl_roundtrip_target_agreement_and_schema(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            result = run(WorkUnit('t'), mixed(second=0), {'x': Fake(), 'next': Fake()}, recorder=JsonlRecorder(path))
            self.assertEqual(tuple(read_events(path)), result.events)
        self.assertEqual(tuple(f.name for f in fields(Event)), ('kind', 'run_id', 'state',
            'state_visit_id', 'invocation_id', 'outcome', 'target', 'trigger_event',
            'metric', 'limit', 'observed', 'occurred_at_unix_ns', 'duration_ns',
            'correlation_id', 'parent_run_id'))
        events = result.events
        with self.assertRaisesRegex(ObservabilityError, 'target disagrees'):
            summarize_runs((*events[:2], replace(events[2], target='OTHER'), *events[3:]))
        self.assertEqual(summarize_runs(events)[0].terminal_state, 'DONE')

    def test_recorder_failure_retains_observed_specific_route(self):
        for kind in ('budget_exceeded', 'transition_selected'):
            persisted = []
            class Recorder:
                def record(self, event):
                    if event.kind == kind: raise OSError('recording failed')
                    persisted.append(event)
            x, nxt = Fake(), Fake()
            with self.subTest(kind=kind), self.assertRaisesRegex(ExecutionError, 'recorder failed') as caught:
                run(WorkUnit('t'), mixed(second=0), {'x': x, 'next': nxt}, recorder=Recorder())
            self.assertEqual(tuple(persisted), caught.exception.events[:-1])
            self.assertEqual(caught.exception.events[-1].target, 'NEXT')
            self.assertEqual((x.calls, nxt.calls), (0, 0))

    def test_executable_fallback_has_ordinary_timing(self):
        with patch('runtime.time.time_ns', return_value=100), patch('runtime.time.monotonic_ns', side_effect=range(11)):
            result = run(WorkUnit('t'), mixed(second=0), {'x': Fake(), 'next': Fake()})
        summary, = summarize_runs(result.events)
        self.assertEqual(summary.invocation_duration_ns_total, 1)
        self.assertEqual(summary.run_duration_ns, 10)
        self.assertEqual(dict(summary.state_duration_ns), {'NEXT': 4, 'DONE': 1})
