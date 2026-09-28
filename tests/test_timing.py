"""Emission timing contracts, with controlled clocks and no sleeps."""
from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from runtime import (Event, ExecutionError, ExecutorResult, Flow, InvocationResponse,
                     State, WorkUnit, run)
from runtime.recording import JsonlRecorder
from switchyard.observability import ObservabilityError, read_events, summarize_runs


class Clock:
    def __init__(self, wall=1000, wall_step=100, step=10):
        self.wall, self.wall_step, self.step = wall, wall_step, step
        self.monotonic = 500

    def sample(self):
        pair = self.wall, self.monotonic
        self.wall += self.wall_step
        self.monotonic += self.step
        return pair


def untimed(events):
    return tuple(replace(e, occurred_at_unix_ns=None, duration_ns=None) for e in events)


def executor(outcome='success'):
    return Mock(run=Mock(return_value=InvocationResponse(ExecutorResult(outcome))))


def simple_flow(outcome='success'):
    return Flow('a', {'a': State('x', {outcome: 'done'}), 'done': State(terminal=True)})


class TimingTests(unittest.TestCase):
    def execute(self, flow=None, executors=None, recorder=None, clock=None):
        with patch('runtime._Clock', return_value=clock or Clock()):
            return run(WorkUnit('private task'), flow or simple_flow(),
                       executors if executors is not None else {'x': executor()}, recorder=recorder)

    def test_terminal_only_exact_timing(self):
        result = self.execute(Flow('done', {'done': State(terminal=True)}), {})
        self.assertEqual([e.kind for e in result.events],
                         ['run_started', 'state_entered', 'state_exited', 'run_finished'])
        self.assertEqual([e.occurred_at_unix_ns for e in result.events], [1000, 1100, 1200, 1300])
        self.assertEqual([e.duration_ns for e in result.events], [None, None, 10, 30])
        summary, = summarize_runs(result.events)
        self.assertEqual(summary.run_duration_ns, 30)
        self.assertEqual(summary.invocation_duration_ns_total, 0)
        self.assertEqual(dict(summary.state_duration_ns), {'done': 10})

    def test_invocation_and_state_intervals(self):
        events = self.execute().events
        self.assertEqual([e.occurred_at_unix_ns for e in events], list(range(1000, 1900, 100)))
        self.assertEqual([e.duration_ns for e in events], [None, None, None, 10, None, 40, None, 10, 80])
        summary, = summarize_runs(events)
        self.assertEqual(summary.run_duration_ns, 80)
        self.assertEqual(summary.invocation_duration_ns_total, 10)
        self.assertEqual(dict(summary.state_duration_ns), {'a': 40, 'done': 10})
        with self.assertRaises(TypeError):
            summary.state_duration_ns['a'] = 99

    def test_bounded_repeated_states_and_budget_timing(self):
        flow = Flow('verify', {
            'verify': State('v', {'failed': 'repair'}),
            'repair': State('r', {'success': 'verify', 'budget_exceeded': 'done'}, max_state_visits_per_run=1),
            'done': State(terminal=True)})
        result = self.execute(flow, {'v': executor('failed'), 'r': executor()})
        summary, = summarize_runs(result.events)
        self.assertEqual(dict(summary.state_visit_counts), {'verify': 2, 'repair': 1, 'done': 1})
        self.assertEqual(dict(summary.state_duration_ns), {'verify': 80, 'repair': 40, 'done': 10})
        self.assertEqual(summary.invocation_duration_ns_total, 30)
        self.assertEqual(summary.run_duration_ns, 200)
        self.assertEqual(summary.budget_exceeded_count, 1)
        self.assertEqual([e.duration_ns for e in result.events if e.trigger_event or e.kind == 'budget_exceeded'], [None, None])

    def test_wall_clock_backwards_changes_no_semantics_or_durations(self):
        with patch('runtime.uuid4', return_value='same'):
            forwards = self.execute(clock=Clock())
            backwards = self.execute(clock=Clock(wall_step=-100))
        self.assertEqual(untimed(forwards.events), untimed(backwards.events))
        self.assertEqual([e.duration_ns for e in forwards.events], [e.duration_ns for e in backwards.events])
        self.assertEqual([e.occurred_at_unix_ns for e in backwards.events], list(range(1000, 100, -100)))
        self.assertEqual(summarize_runs(forwards.events), summarize_runs(backwards.events))

    def test_clock_boundary_uses_integer_system_clocks(self):
        with patch('runtime.time.time_ns', return_value=123) as wall, patch('runtime.time.monotonic_ns', side_effect=range(100, 109)) as monotonic:
            result = run(WorkUnit('t'), simple_flow(), {'x': executor()})
        self.assertEqual(wall.call_count, len(result.events))
        self.assertEqual(monotonic.call_count, len(result.events))
        self.assertTrue(all(e.occurred_at_unix_ns == 123 for e in result.events))
        self.assertEqual(result.events[-1].duration_ns, 8)

    def test_timeout_outcome_is_still_normal_completion(self):
        result = self.execute(simple_flow('timeout'), {'x': executor('timeout')})
        summary, = summarize_runs(result.events)
        self.assertTrue(summary.completed_normally)
        self.assertEqual(dict(summary.outcome_counts), {'timeout': 1})
        self.assertEqual((summary.invocation_duration_ns_total, summary.run_duration_ns), (10, 80))

    def test_executor_exception_and_undeclared_outcome_run_durations(self):
        bug = ValueError('executor failed')
        for flow, adapter, kinds, duration, invocation_total in (
            (simple_flow(), Mock(run=Mock(side_effect=bug)),
             ['run_started', 'state_entered', 'invocation_started', 'run_error'], 30, None),
            (Flow('a', {'a': State('x'), 'done': State(terminal=True)}), executor(),
             ['run_started', 'state_entered', 'invocation_started', 'run_error'], 30, None),
        ):
            with self.subTest(kinds=kinds):
                with self.assertRaises(ExecutionError) as caught:
                    self.execute(flow, {'x': adapter})
                events = caught.exception.events
                self.assertEqual([e.kind for e in events], kinds)
                self.assertEqual(events[-1].duration_ns, duration)
                self.assertEqual(events[-1].occurred_at_unix_ns, 1000 + 100 * (len(events)-1))
                summary, = summarize_runs(events)
                self.assertEqual(summary.run_duration_ns, duration)
                self.assertEqual(summary.invocation_duration_ns_total, invocation_total)
                self.assertEqual(dict(summary.state_duration_ns), {'a': None})
                self.assertFalse(summary.completed_normally)
                self.assertIs(caught.exception.__cause__, bug if adapter.run.side_effect is bug else None)

    def test_unhandled_budget_error_has_run_duration(self):
        flow = Flow('a', {'a': State('x', max_state_visits_per_run=0), 'done': State(terminal=True)})
        adapter = executor()
        with self.assertRaises(ExecutionError) as caught:
            self.execute(flow, {'x': adapter})
        adapter.run.assert_not_called()
        self.assertEqual([e.kind for e in caught.exception.events], ['run_started', 'budget_exceeded', 'run_error'])
        summary, = summarize_runs(caught.exception.events)
        self.assertEqual(summary.run_duration_ns, 20)
        self.assertEqual(summary.invocation_duration_ns_total, 0)
        self.assertEqual(dict(summary.state_duration_ns), {})

    def test_recorder_latency_included_until_closing_emission(self):
        clock = Clock(step=0)
        def record(event):
            clock.monotonic += 7
        result = self.execute(recorder=Mock(record=Mock(side_effect=record)), clock=clock)
        self.assertEqual([e.duration_ns for e in result.events], [None, None, None, 7, None, 28, None, 7, 56])
        self.assertEqual(clock.monotonic, 563)  # final persistence is outside run duration

    def test_recorder_failure_keeps_observed_event_and_disk_prefix_distinct(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            baseline = self.execute().events
            original = JsonlRecorder.record
            for position in range(len(baseline)):
                with self.subTest(position=position):
                    path.write_bytes(b'')
                    disk_error = OSError('disk failed')
                    calls = []
                    def record(recorder, event):
                        calls.append(event)
                        if len(calls) == position + 1:
                            raise disk_error
                        original(recorder, event)
                    with patch.object(JsonlRecorder, 'record', record), self.assertRaises(ExecutionError) as caught:
                        self.execute(recorder=JsonlRecorder(path))
                    observed = caught.exception.events
                    self.assertIs(caught.exception.__cause__, disk_error)
                    self.assertIsNone(caught.exception.recorder_error)
                    self.assertEqual(observed, tuple(calls))
                    self.assertEqual(tuple(read_events(path)), observed[:-1])
                    self.assertEqual([e.kind for e in observed], [e.kind for e in baseline[:position+1]])
                    self.assertEqual([e.duration_ns for e in observed], [e.duration_ns for e in baseline[:position+1]])
                    self.assertTrue(all(e.occurred_at_unix_ns is not None for e in observed))
                    summary, = summarize_runs(observed)
                    self.assertEqual(summary.run_duration_ns, 80 if position == len(baseline)-1 else None)
                    if position:
                        persisted, = summarize_runs(read_events(path))
                        self.assertIsNone(persisted.run_duration_ns)
                        self.assertFalse(persisted.completed_normally)

    def test_run_error_recording_failure_preserves_primary_cause(self):
        bug, disk_error = ValueError('executor failed'), OSError('disk failed')
        observed = []
        def record(event):
            observed.append(event)
            if event.kind == 'run_error':
                raise disk_error
        with self.assertRaises(ExecutionError) as caught:
            self.execute(executors={'x': Mock(run=Mock(side_effect=bug))}, recorder=Mock(record=record))
        self.assertIs(caught.exception.__cause__, bug)
        self.assertIs(caught.exception.recorder_error, disk_error)
        self.assertEqual(caught.exception.events, tuple(observed))
        self.assertEqual(observed[-1].duration_ns, 30)

    def test_incomplete_and_partially_timed_intervals_are_unavailable(self):
        events = self.execute().events
        for length in (3, 4, 6, 7, 8):
            with self.subTest(length=length):
                summary, = summarize_runs(events[:length])
                self.assertIsNone(summary.run_duration_ns)
                self.assertEqual(summary.invocation_duration_ns_total, None if length == 3 else 10)
                self.assertEqual(summary.state_duration_ns['a'], 40 if length >= 6 else None)
                if length >= 7:
                    self.assertEqual(summary.state_duration_ns['done'], 10 if length == 8 else None)
        partial = tuple(replace(e, duration_ns=None) if e.kind in ('invocation_finished', 'state_exited') else e for e in events)
        summary, = summarize_runs(partial)
        self.assertEqual(summary.run_duration_ns, 80)
        self.assertIsNone(summary.invocation_duration_ns_total)
        self.assertEqual(dict(summary.state_duration_ns), {'a': None, 'done': None})

    def test_historical_and_new_appended_jsonl_roundtrip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            old_events = untimed(self.execute().events)
            old_records = [{k: v for k, v in asdict(e).items() if k not in ('occurred_at_unix_ns', 'duration_ns')} for e in old_events]
            path.write_text(''.join(json.dumps(record) + '\n' for record in old_records))
            old_prefix = path.read_bytes()
            new = self.execute(recorder=JsonlRecorder(path)).events
            self.assertTrue(path.read_bytes().startswith(old_prefix))
            self.assertEqual(tuple(read_events(path)), old_events + new)
            old_summary, new_summary = summarize_runs(read_events(path))
            self.assertIsNone(old_summary.run_duration_ns)
            self.assertIsNone(old_summary.invocation_duration_ns_total)
            self.assertEqual(dict(old_summary.state_duration_ns), {'a': None, 'done': None})
            self.assertEqual(new_summary.run_duration_ns, 80)
            self.assertEqual(new_summary.invocation_duration_ns_total, 10)
            records = [json.loads(line) for line in path.read_bytes()[len(old_prefix):].splitlines()]
            self.assertTrue(all('duration_ns' in r and 'occurred_at_unix_ns' in r for r in records))
            self.assertEqual(records, [asdict(e) for e in new])

    def test_zero_is_distinct_from_unavailable_and_optional_fields(self):
        events = self.execute(Flow('done', {'done': State(terminal=True)}), {}, clock=Clock(step=0)).events
        current, = summarize_runs(events)
        historical, = summarize_runs(untimed(events))
        self.assertEqual((current.run_duration_ns, current.invocation_duration_ns_total), (0, 0))
        self.assertEqual(dict(current.state_duration_ns), {'done': 0})
        self.assertEqual((historical.run_duration_ns, historical.invocation_duration_ns_total), (None, None))
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            for omitted in ((), ('occurred_at_unix_ns',), ('duration_ns',), ('occurred_at_unix_ns', 'duration_ns')):
                record = asdict(Event('run_started', 'r', occurred_at_unix_ns=-1))
                for name in omitted:
                    record.pop(name)
                path.write_text(json.dumps(record) + '\n')
                event, = read_events(path)
                self.assertEqual(event.occurred_at_unix_ns, None if 'occurred_at_unix_ns' in omitted else -1)
                self.assertIsNone(event.duration_ns)

    def test_timing_validation_keeps_strict_record_boundary(self):
        finish = self.execute().events[-1]
        bad = [replace(finish, **{field: value})
               for field in ('occurred_at_unix_ns', 'duration_ns')
               for value in (True, 1.5, '10', [], {})]
        bad += [replace(finish, duration_ns=-1), Event('run_started', 'r', duration_ns=0)]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            for event in bad:
                with self.subTest(event=event):
                    path.write_text(json.dumps(asdict(event)) + '\n')
                    with self.assertRaises(ObservabilityError):
                        tuple(read_events(path))
                    with self.assertRaises(ObservabilityError):
                        summarize_runs([event])
            record = asdict(Event('run_started', 'r'))
            record['monotonic_ns'] = 0
            path.write_text(json.dumps(record) + '\n')
            with self.assertRaises(ObservabilityError):
                tuple(read_events(path))

    def test_interleaved_runs_never_share_timing(self):
        first = self.execute(clock=Clock(step=10)).events
        second = self.execute(clock=Clock(step=100)).events
        events = [e for pair in zip(first, second) for e in pair]
        a, b = summarize_runs(events)
        self.assertEqual((a.run_duration_ns, b.run_duration_ns), (80, 800))
        self.assertEqual((a.invocation_duration_ns_total, b.invocation_duration_ns_total), (10, 100))
        self.assertEqual((a.state_duration_ns['a'], b.state_duration_ns['a']), (40, 400))
