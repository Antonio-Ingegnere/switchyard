"""Admitted visits survive invocation refusal; no compiler required."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runtime import (DiagnosticPayload, ExecutionError, ExecutorResult, Flow,
                     InvocationBudgetScope as Scope, InvocationResponse, State, WorkUnit, run)
from runtime.recording import JsonlRecorder
from switchyard.observability import ObservabilityError, read_events, summarize_runs


class Fake:
    def __init__(self, outcome='ready', diagnostics=None):
        self.calls = 0
        self.outcome, self.diagnostics = outcome, diagnostics

    def run(self, request):
        self.calls += 1
        return InvocationResponse(ExecutorResult(self.outcome), self.diagnostics)


def refused_flow(kind='scope'):
    return Flow('A', {
        'A': State('x', {'ready': 'STOP'}, max_state_visits_per_run=0 if kind == 'visit' else 2,
                   state_visit_budget_exceeded='STOP',
                   invocation_budgets=(Scope('local', 0 if kind == 'scope' else 2, 'STOP'),)),
        'STOP': State(terminal=True)}, 0 if kind == 'run' else 2, 'STOP')


class TwoPhaseAdmissionTests(unittest.TestCase):
    def test_invocation_refusal_commits_visit_without_invocation(self):
        for kind in ('run', 'scope'):
            with self.subTest(kind=kind):
                fake = Fake()
                result = run(WorkUnit('t'), refused_flow(kind), {'x': fake})
                self.assertEqual([e.kind for e in result.events], ['run_started', 'state_entered',
                    'budget_exceeded', 'transition_selected', 'state_exited',
                    'state_entered', 'state_exited', 'run_finished'])
                visit = result.events[1].state_visit_id
                self.assertIsNotNone(visit)
                self.assertTrue(all(e.state_visit_id == visit for e in result.events[1:5]))
                self.assertTrue(all(e.invocation_id is None for e in result.events))
                self.assertNotEqual(visit, result.events[5].state_visit_id)
                self.assertEqual(fake.calls, 0)
                summary, = summarize_runs(result.events)
                self.assertEqual(dict(summary.state_visit_counts), {'A': 1, 'STOP': 1})
                self.assertEqual((summary.invocation_count, summary.budget_exceeded_count), (0, 1))

    def test_visit_refusal_still_has_no_visit(self):
        result = run(WorkUnit('t'), refused_flow('visit'), {'x': Fake()})
        self.assertEqual([e.kind for e in result.events], ['run_started', 'budget_exceeded',
            'transition_selected', 'state_entered', 'state_exited', 'run_finished'])
        self.assertIsNone(result.events[1].state_visit_id)
        self.assertIsNone(result.events[2].state_visit_id)
        self.assertEqual(dict(summarize_runs(result.events)[0].state_visit_counts), {'STOP': 1})

    def test_previous_compiler_blocker_exact_probe(self):
        flow = Flow('A', {
            'A': State('a', {'ready': 'RETURN'}, max_state_visits_per_run=2,
                state_visit_budget_exceeded='STOP', invocation_budgets=(Scope('primary', 1, 'B'),)),
            'B': State('b', {'ready': 'RETURN'}, invocation_budgets=(Scope('alternate', 2, 'STOP'),)),
            'RETURN': State('back', {'ready': 'A'}, max_state_visits_per_run=3,
                            state_visit_budget_exceeded='STOP'),
            'STOP': State(terminal=True)}, 10, 'STOP')
        a, b, back = Fake(), Fake(), Fake()
        result = run(WorkUnit('t'), flow, {'a': a, 'b': b, 'back': back})
        self.assertEqual([e.state for e in result.events if e.kind == 'invocation_started'],
                         ['A', 'RETURN', 'B', 'RETURN'])
        self.assertEqual((a.calls, b.calls, back.calls), (1, 1, 2))
        budgets = [e for e in result.events if e.kind == 'budget_exceeded']
        self.assertEqual([(e.metric, e.observed, e.target) for e in budgets],
                         [('invocation_scope:primary', 1, 'B'), ('max_state_visits_per_run', 2, 'STOP')])
        self.assertIsNotNone(budgets[0].state_visit_id)
        self.assertIsNone(budgets[1].state_visit_id)
        summary, = summarize_runs(result.events)
        self.assertEqual(dict(summary.state_visit_counts), {'A': 2, 'RETURN': 2, 'B': 1, 'STOP': 1})
        self.assertEqual(summary.invocation_count, 4)
        started = [e.invocation_id for e in result.events if e.kind == 'invocation_started']
        self.assertTrue(all(value.endswith(f':invocation:{i}') for i, value in enumerate(started, 1)))

    def test_state_duration_without_invocation_duration(self):
        with patch('runtime.time.time_ns', return_value=10), patch('runtime.time.monotonic_ns', side_effect=range(0, 80, 10)):
            result = run(WorkUnit('t'), refused_flow(), {'x': Fake()})
        self.assertEqual(result.events[4].duration_ns, 30)
        self.assertTrue(all(e.duration_ns is None for e in result.events[1:4]))
        summary, = summarize_runs(result.events)
        self.assertEqual(dict(summary.state_duration_ns), {'A': 30, 'STOP': 10})
        self.assertEqual(summary.invocation_count, 0)
        self.assertFalse(any(e.kind.startswith('invocation_') for e in result.events))

    def test_new_and_historical_jsonl_refusals_remain_readable(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'events.jsonl'
            result = run(WorkUnit('t'), refused_flow(), {'x': Fake()}, recorder=JsonlRecorder(path))
            self.assertEqual(tuple(read_events(path)), result.events)
            old_path = Path(folder) / 'historical.jsonl'
            recorder = JsonlRecorder(old_path)
            # Historical invocation refusal occurred before admission and had no visit IDs.
            historical = [e for e in result.events if not (e.state == 'A' and e.kind in ('state_entered', 'state_exited'))]
            for event in historical:
                recorder.record(replace(event, state_visit_id=None) if event.state == 'A' else event)
            summary, = summarize_runs(read_events(old_path))
            self.assertEqual(dict(summary.state_visit_counts), {'STOP': 1})
            self.assertEqual(summary.budget_exceeded_count, 1)

    def test_reader_rejects_inconsistent_admitted_refusal(self):
        events = run(WorkUnit('t'), refused_flow(), {'x': Fake()}).events
        for bad in (
            (*events[:2], replace(events[2], state_visit_id=None), *events[3:]),
            (*events[:3], replace(events[3], state_visit_id='wrong'), *events[4:]),
            (*events[:2], replace(events[2], metric='max_state_visits_per_run'), *events[3:]),
            (*events[:3], events[4], events[3], *events[5:]),
            (*events[:4], replace(events[1], kind='invocation_started', invocation_id='fake'), *events[4:]),
        ):
            with self.subTest(events=bad), self.assertRaises(ObservabilityError): summarize_runs(bad)

    def test_missing_route_closes_visit_then_errors(self):
        flow = Flow('A', {'A': State('x', invocation_budgets=(Scope('empty', 0),)), 'STOP': State(terminal=True)})
        with self.assertRaisesRegex(ExecutionError, 'missing transition') as caught:
            run(WorkUnit('t'), flow, {'x': Fake()})
        self.assertEqual([e.kind for e in caught.exception.events], ['run_started', 'state_entered',
            'budget_exceeded', 'state_exited', 'run_error'])
        summary, = summarize_runs(caught.exception.events)
        self.assertFalse(summary.completed_normally)
        self.assertEqual(dict(summary.state_visit_counts), {'A': 1})

    def test_recorder_failure_does_not_close_visit_or_roll_back(self):
        for kind in ('state_entered', 'budget_exceeded', 'transition_selected', 'state_exited'):
            persisted = []
            class Recorder:
                def record(self, event):
                    if event.state == 'A' and event.kind == kind: raise OSError('unavailable')
                    persisted.append(event)
            with self.subTest(kind=kind), self.assertRaisesRegex(ExecutionError, 'recorder failed') as caught:
                run(WorkUnit('t'), refused_flow(), {'x': Fake()}, recorder=Recorder())
            self.assertEqual(tuple(persisted), caught.exception.events[:-1])
            self.assertEqual(caught.exception.events[-1].kind, kind)
            summary, = summarize_runs(caught.exception.events)
            self.assertEqual(dict(summary.state_visit_counts), {'A': 1})
            self.assertEqual(summary.invocation_count, 0)
            self.assertFalse(summary.completed_normally)

    def test_diagnostic_admission_still_precedes_entry(self):
        flow = Flow('START', {'START': State('s', {'ready': 'A'}),
            'A': State('x', diagnostics_from_previous_max_bytes=1, invocation_budgets=(Scope('empty', 0),)),
            'STOP': State(terminal=True)})
        with self.assertRaisesRegex(ExecutionError, 'diagnostic input exceeds') as caught:
            run(WorkUnit('t'), flow, {'s': Fake(diagnostics=DiagnosticPayload(b'too long')), 'x': Fake()})
        self.assertFalse(any(e.state == 'A' and e.kind == 'state_entered' for e in caught.exception.events))
