"""Real admission refusals, with ordinary executable fallback and no new counters."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import (DiagnosticPayload, ExecutionError, ExecutorResult, Flow,
                     FlowValidationError, InvocationResponse, State, WorkUnit, run)
from runtime.recording import JsonlRecorder
from switchyard.observability import read_events, summarize_runs


class Fake:
    def __init__(self, outcome='ready', diagnostics=None):
        self.outcome = outcome
        self.diagnostics = diagnostics
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return InvocationResponse(ExecutorResult(self.outcome), self.diagnostics)


def refused(target, limit=0):
    return State('refused', {'budget_exceeded': target}, max_state_visits_per_run=limit)


def fallback_flow():
    return Flow('A', {'A': refused('B'), 'B': State('b', {'ready': 'DONE'}),
                      'DONE': State(terminal=True)})


class BudgetRoutingTests(unittest.TestCase):
    def test_executable_fallback_exact_events_and_admission_identity(self):
        flow = Flow('START', {'START': State('start', {'ready': 'A'}), **fallback_flow().states})
        start, unused, fallback = Fake(), Fake(), Fake()
        result = run(WorkUnit('task'), flow, {'start': start, 'refused': unused, 'b': fallback})
        self.assertEqual(result.terminal_state, 'DONE')
        self.assertEqual([len(x.requests) for x in (start, unused, fallback)], [1, 0, 1])
        self.assertEqual([(e.kind, e.state) for e in result.events], [
            ('run_started', None), ('state_entered', 'START'), ('invocation_started', 'START'),
            ('invocation_finished', 'START'), ('transition_selected', 'START'), ('state_exited', 'START'),
            ('budget_exceeded', 'A'), ('transition_selected', 'A'),
            ('state_entered', 'B'), ('invocation_started', 'B'), ('invocation_finished', 'B'),
            ('transition_selected', 'B'), ('state_exited', 'B'),
            ('state_entered', 'DONE'), ('state_exited', 'DONE'), ('run_finished', 'DONE')])
        budget, route = result.events[6:8]
        self.assertEqual((budget.metric, budget.limit, budget.observed, budget.target),
                         ('max_state_visits_per_run', 0, 0, None))
        self.assertEqual((route.trigger_event, route.target), ('budget_exceeded', 'B'))
        for event in (budget, route):
            self.assertIsNone(event.state_visit_id)
            self.assertIsNone(event.invocation_id)
            self.assertIsNone(event.outcome)
            self.assertIsNone(event.duration_ns)
        rid = result.events[0].run_id
        self.assertEqual([e.state_visit_id for e in result.events if e.kind == 'state_entered'],
                         [f'{rid}:visit:{i}' for i in (1, 2, 3)])
        self.assertEqual([e.invocation_id for e in result.events if e.kind == 'invocation_started'],
                         [f'{rid}:invocation:{i}' for i in (1, 2)])
        self.assertEqual(result.events[10].outcome, 'ready')
        self.assertEqual(result.events[11].outcome, 'ready')

    def test_chained_refusal_roundtrip_and_projection(self):
        flow = Flow('A', {'A': refused('B'), 'B': refused('C'),
                          'C': State('c', {'ready': 'DONE'}), 'DONE': State(terminal=True)})
        unused, fallback = Fake(), Fake()
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            result = run(WorkUnit('task'), flow, {'refused': unused, 'c': fallback}, recorder=JsonlRecorder(path))
            events = tuple(read_events(path))
        self.assertEqual(events, result.events)
        self.assertEqual(len(unused.requests), 0)
        self.assertEqual(len(fallback.requests), 1)
        self.assertEqual([(e.kind, e.state, e.target) for e in events[1:5]], [
            ('budget_exceeded', 'A', None), ('transition_selected', 'A', 'B'),
            ('budget_exceeded', 'B', None), ('transition_selected', 'B', 'C')])
        for event in events[1:5]:
            self.assertIsNone(event.state_visit_id)
            self.assertIsNone(event.invocation_id)
        self.assertTrue(events[5].state_visit_id.endswith(':visit:1'))
        self.assertTrue(events[6].invocation_id.endswith(':invocation:1'))
        summary, = summarize_runs(events)
        self.assertEqual(dict(summary.state_visit_counts), {'C': 1, 'DONE': 1})
        self.assertEqual(summary.budget_exceeded_count, 2)
        self.assertEqual(summary.invocation_count, 1)

    def test_fallback_keeps_previously_consumed_allowance(self):
        # B executes once, then A refuses back to B. B must now use its own route.
        flow = Flow('B', {'A': refused('B'),
                          'B': State('b', {'ready': 'A', 'budget_exceeded': 'C'}, max_state_visits_per_run=1),
                          'C': State('c', {'ready': 'DONE'}), 'DONE': State(terminal=True)})
        unused, b, c = Fake(), Fake(), Fake()
        result = run(WorkUnit('task'), flow, {'refused': unused, 'b': b, 'c': c})
        self.assertEqual([len(x.requests) for x in (unused, b, c)], [0, 1, 1])
        self.assertEqual([(e.state, e.limit, e.observed) for e in result.events if e.kind == 'budget_exceeded'],
                         [('A', 0, 0), ('B', 1, 1)])
        self.assertEqual([e.target for e in result.events if e.trigger_event == 'budget_exceeded'], ['B', 'C'])
        ids = [e.state_visit_id for e in result.events if e.kind == 'state_entered']
        self.assertEqual(len(ids), len(set(ids)))

    def test_refused_fallback_without_handler_uses_existing_error(self):
        flow = Flow('A', {'A': refused('B'), 'B': State('b', max_state_visits_per_run=0),
                          'DONE': State(terminal=True)})
        unused, b = Fake(), Fake()
        with self.assertRaisesRegex(ExecutionError, 'missing transition: B.budget_exceeded') as caught:
            run(WorkUnit('task'), flow, {'refused': unused, 'b': b})
        self.assertEqual([(e.kind, e.state) for e in caught.exception.events], [
            ('run_started', None), ('budget_exceeded', 'A'), ('transition_selected', 'A'),
            ('budget_exceeded', 'B'), ('run_error', 'B')])
        self.assertEqual(len(unused.requests) + len(b.requests), 0)
        self.assertIsNone(caught.exception.events[-1].state_visit_id)

    def test_refusal_cycles_rejected_before_execution(self):
        for cycle in ({'A': refused('A', 1)},
                      {'A': refused('B', 1), 'B': refused('A', 1)},
                      {'A': refused('B'), 'B': State('refused', {'ready': 'A'})}):
            for initial in ('A', 'DONE'):  # Unreachable components are validated too.
                with self.subTest(cycle=cycle, initial=initial):
                    fake = Fake()
                    with self.assertRaisesRegex(FlowValidationError, 'cycle without a bounded state admission'):
                        run(WorkUnit('task'), Flow(initial, {**cycle, 'DONE': State(terminal=True)}), {'refused': fake})
                    self.assertEqual(fake.requests, [])

    def test_bounded_semantic_cycle_with_executable_fallback_terminates(self):
        flow = Flow('A', {'A': State('a', {'ready': 'A', 'budget_exceeded': 'B'}, max_state_visits_per_run=2),
                          'B': State('b', {'ready': 'DONE'}), 'DONE': State(terminal=True)})
        a, b = Fake(), Fake()
        result = run(WorkUnit('task'), flow, {'a': a, 'b': b})
        self.assertEqual([len(x.requests) for x in (a, b)], [2, 1])
        self.assertEqual(result.terminal_state, 'DONE')

    def test_terminal_refusal_sequence_unchanged(self):
        unused = Fake()
        result = run(WorkUnit('task'), Flow('A', {'A': refused('DONE'), 'DONE': State(terminal=True)}), {'refused': unused})
        self.assertEqual([e.kind for e in result.events], ['run_started', 'budget_exceeded',
            'transition_selected', 'state_entered', 'state_exited', 'run_finished'])
        self.assertEqual(unused.requests, [])
        self.assertEqual(result.events[2].target, 'DONE')
        self.assertTrue(result.events[3].state_visit_id.endswith(':visit:1'))

    def test_refusal_clears_diagnostics_before_fallback(self):
        flow = Flow('START', {'START': State('start', {'ready': 'A'}), 'A': refused('B'),
                              'B': State('b', {'ready': 'DONE'}, diagnostics_from_previous_max_bytes=1),
                              'DONE': State(terminal=True)})
        start = Fake(diagnostics=DiagnosticPayload(b'old evidence'))
        unused, b = Fake(), Fake()
        run(WorkUnit('task'), flow, {'start': start, 'refused': unused, 'b': b})
        self.assertIsNone(b.requests[0].diagnostics)
        self.assertEqual(b.requests[0].work_unit, WorkUnit('task'))

    def test_refusal_has_no_duration_fallback_has_normal_timing(self):
        with patch('runtime.time.time_ns', return_value=1000), patch('runtime.time.monotonic_ns', side_effect=range(0, 110, 10)):
            result = run(WorkUnit('task'), fallback_flow(), {'refused': Fake(), 'b': Fake()})
        self.assertTrue(all(e.occurred_at_unix_ns == 1000 for e in result.events))
        self.assertEqual([e.duration_ns for e in result.events[1:3]], [None, None])
        summary, = summarize_runs(result.events)
        self.assertEqual(summary.invocation_duration_ns_total, 10)
        self.assertEqual(dict(summary.state_duration_ns), {'B': 40, 'DONE': 10})
        self.assertEqual(summary.run_duration_ns, 100)

    def test_recorder_failure_observed_event_is_not_persisted_or_retried(self):
        for failed_kind, failed_state in [('budget_exceeded', 'A'), ('transition_selected', 'A'), ('state_entered', 'B')]:
            with self.subTest(kind=failed_kind):
                persisted = []
                class Recorder:
                    def record(self, event):
                        if (event.kind, event.state) == (failed_kind, failed_state):
                            raise OSError('recording unavailable')
                        persisted.append(event)
                unused, b = Fake(), Fake()
                with self.assertRaisesRegex(ExecutionError, 'recorder failed recording') as caught:
                    run(WorkUnit('task'), fallback_flow(), {'refused': unused, 'b': b}, recorder=Recorder())
                events = caught.exception.events
                self.assertEqual(tuple(persisted), events[:-1])
                self.assertEqual((events[-1].kind, events[-1].state), (failed_kind, failed_state))
                self.assertFalse(any(e.kind == 'run_error' for e in events))
                self.assertEqual(unused.requests + b.requests, [])

    def test_executor_cannot_spoof_executable_budget_route(self):
        flow = Flow('A', {'A': State('a', {'budget_exceeded': 'B'}, max_state_visits_per_run=1),
                          'B': State('b', {'ready': 'DONE'}), 'DONE': State(terminal=True)})
        b = Fake()
        with self.assertRaisesRegex(ExecutionError, 'reserved control-plane outcome') as caught:
            run(WorkUnit('task'), flow, {'a': Fake('budget_exceeded'), 'b': b})
        self.assertEqual(b.requests, [])
        self.assertFalse(any(e.kind in ('budget_exceeded', 'transition_selected') for e in caught.exception.events))
