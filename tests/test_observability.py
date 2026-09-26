from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from runtime import (Event, ExecutionError, ExecutorResult, Flow, InvocationResponse,
                     State, WorkUnit, run)
from runtime.command import Command, CommandExecutor
from runtime.recording import JsonlRecorder
from switchyard.observability import (ObservabilityError, RunSummary, read_events,
                                     summarize_runs)


class Outcomes:
    def __init__(self, *outcomes):
        self.outcomes = iter(outcomes)

    def run(self, request):
        return InvocationResponse(ExecutorResult(next(self.outcomes)))


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'events.jsonl'

    def terminal(self, name='done', recorder=None):
        return run(WorkUnit('private task'), Flow(name, {name: State(terminal=True)}),
                   {}, recorder=recorder)

    def cycle(self, recorder=None):
        flow = Flow('check', {
            'check': State('x', {'failed': 'again', 'success': 'done'}),
            'again': State('x', {'success': 'check', 'budget_exceeded': 'stopped'},
                           max_state_visits_per_run=1),
            'done': State(terminal=True), 'stopped': State(terminal=True)})
        return run(WorkUnit('private task'), flow, {'x': Outcomes('failed', 'success', 'failed')},
                   recorder=recorder)

    def test_terminal_names_have_no_special_meaning(self):
        for name in ('accepted', 'escalated', 'failed', 'ready_for_manual_qa', 'repair', '名字\n'):
            with self.subTest(name=name):
                result = self.terminal(name)
                summary, = summarize_runs(iter(result.events))
                self.assertEqual(summary.run_id, result.events[0].run_id)
                self.assertEqual(summary.terminal_state, name)
                self.assertTrue(summary.completed_normally)
                self.assertEqual(dict(summary.state_visit_counts), {name: 1})
                self.assertEqual(summary.invocation_count, 0)
                self.assertEqual(dict(summary.invocation_counts_by_state), {})
                self.assertEqual(dict(summary.outcome_counts), {})
                self.assertEqual((summary.transition_count, summary.budget_exceeded_count), (0, 0))
                self.assertFalse(hasattr(summary, 'repair_count'))

    def test_real_command_success_and_failed_outcome(self):
        for exit_code, outcome in ((0, 'success'), (1, 'failed')):
            executor = CommandExecutor(Command(sys.executable, ('-c', f'raise SystemExit({exit_code})'),
                                                str(self.root), 5))
            result = run(WorkUnit('t'), Flow('repair', {
                'repair': State('cmd', {outcome: 'failed'}),
                'failed': State(terminal=True)}), {'cmd': executor})
            summary, = summarize_runs(result.events)
            self.assertEqual(dict(summary.state_visit_counts), {'repair': 1, 'failed': 1})
            self.assertEqual(dict(summary.invocation_counts_by_state), {'repair': 1})
            self.assertEqual(dict(summary.outcome_counts), {outcome: 1})
            self.assertEqual((summary.invocation_count, summary.transition_count), (1, 1))
            self.assertTrue(summary.completed_normally)
            self.assertEqual(summary.terminal_state, 'failed')

    def test_bounded_cycle_repeated_visits_outcomes_and_budget(self):
        result = self.cycle()
        summary, = summarize_runs(result.events)
        self.assertEqual(dict(summary.state_visit_counts), {'check': 2, 'again': 1, 'stopped': 1})
        self.assertEqual(dict(summary.invocation_counts_by_state), {'check': 2, 'again': 1})
        self.assertEqual(dict(summary.outcome_counts), {'failed': 2, 'success': 1})
        self.assertEqual(summary.invocation_count, 3)
        self.assertEqual(summary.transition_count, 4)
        self.assertEqual(summary.budget_exceeded_count, 1)
        self.assertTrue(summary.completed_normally)
        self.assertEqual(summary.terminal_state, 'stopped')

    def test_every_valid_prefix_is_incomplete_until_finished(self):
        events = self.cycle().events
        self.assertEqual(summarize_runs([]), ())
        for length in range(1, len(events)):
            with self.subTest(length=length):
                summary, = summarize_runs(events[:length])
                self.assertIsNone(summary.terminal_state)
                self.assertFalse(summary.completed_normally)
                self.assertEqual(summary.invocation_count,
                                 sum(e.kind == 'invocation_started' for e in events[:length]))
                self.assertEqual(sum(summary.outcome_counts.values()),
                                 sum(e.kind == 'invocation_finished' for e in events[:length]))
        self.assertTrue(summarize_runs(events)[0].completed_normally)

    def test_execution_errors_are_not_normal_completion(self):
        flows = [
            Flow('a', {'a': State('x'), 'done': State(terminal=True)}),
            Flow('a', {'a': State('x', max_state_visits_per_run=0), 'done': State(terminal=True)}),
            Flow('a', {'a': State('x', {'success': 'done'}), 'done': State(terminal=True)}),
        ]
        for flow, executor in zip(flows, [Outcomes('failed'), Outcomes(), Outcomes()]):
            with self.subTest(flow=flow):
                with self.assertRaises(ExecutionError) as caught:
                    run(WorkUnit('t'), flow, {'x': executor})
                summary, = summarize_runs(caught.exception.events)
                self.assertFalse(summary.completed_normally)
                self.assertIsNone(summary.terminal_state)
                with self.assertRaises(ObservabilityError):
                    summarize_runs(caught.exception.events + (caught.exception.events[-1],))

    def test_jsonl_roundtrip_append_and_order(self):
        recorder = JsonlRecorder(self.path)
        first = self.cycle(recorder)
        second = self.terminal('repair', recorder)
        events = first.events + second.events
        self.assertEqual(tuple(read_events(self.path)), events)
        summaries = summarize_runs(read_events(self.path))
        self.assertEqual(summaries, summarize_runs(events))
        self.assertEqual([s.run_id for s in summaries], [first.events[0].run_id, second.events[0].run_id])
        self.assertEqual([s.terminal_state for s in summaries], ['stopped', 'repair'])

    def test_interleaved_runs_preserve_first_seen_order(self):
        first = self.terminal('a').events
        second = self.terminal('b').events
        interleaved = tuple(event for pair in zip(second, first) for event in pair)
        recorder = JsonlRecorder(self.path)
        for event in interleaved:
            recorder.record(event)
        self.assertEqual(tuple(read_events(self.path)), interleaved)
        self.assertEqual(summarize_runs(read_events(self.path)), summarize_runs(second + first))

    def test_incomplete_recorded_history_and_empty_file(self):
        self.path.write_bytes(b'')
        self.assertEqual(summarize_runs(read_events(self.path)), ())
        events = self.cycle().events[:3]
        recorder = JsonlRecorder(self.path)
        for event in events:
            recorder.record(event)
        summary, = summarize_runs(read_events(self.path))
        self.assertEqual((summary.invocation_count, dict(summary.outcome_counts)), (1, {}))
        self.assertIsNone(summary.terminal_state)
        self.assertFalse(summary.completed_normally)

    def test_invalid_jsonl_is_not_skipped_or_repaired(self):
        valid = json.dumps(asdict(Event('run_started', 'r'))).encode()
        bad = [b'{\n', b'null\n', b'[]\n', b'42\n', b'"text"\n', b'\n',
               b'{"kind":', valid, b'\xff\n', b'{}\n', b'NaN\n',
               valid[:-1] + b',"run_id":"duplicate"}\n',
               valid.replace(b'null', b'NaN', 1) + b'\n']
        for line in bad:
            with self.subTest(line=line):
                self.path.write_bytes(valid + b'\n' + line)
                reader = read_events(self.path)
                self.assertEqual(next(reader), Event('run_started', 'r'))
                with self.assertRaisesRegex(ObservabilityError, '^line 2:'):
                    next(reader)
                with self.assertRaises(ObservabilityError):
                    summarize_runs(read_events(self.path))
                self.assertEqual(self.path.read_bytes(), valid + b'\n' + line)

    def test_missing_and_extra_fields(self):
        base = asdict(Event('run_started', 'r'))
        records = [dict(base, extra=None)]
        for key in base.keys() - {'occurred_at_unix_ns', 'duration_ns'}:
            records.append({name: value for name, value in base.items() if name != key})
        for record in records:
            with self.subTest(record=record):
                self.path.write_text(json.dumps(record) + '\n')
                with self.assertRaisesRegex(ObservabilityError, 'missing or unexpected'):
                    tuple(read_events(self.path))

    def test_invalid_field_types_and_shapes_both_input_forms(self):
        invalid = [
            Event('unknown', 'r'), Event('run_started', ''), Event('run_started', None),
            Event('run_started', 'r', state='extra'), Event('run_started', 'r', limit=True),
            Event('run_started', 42), Event('state_entered', 'r', state='a'),
            Event('invocation_started', 'r', 'a', 'v'),
            Event('invocation_finished', 'r', 'a', 'v', 'i', 'invented'),
            Event('transition_selected', 'r', 'a', target='b', trigger_event='unknown'),
            Event('budget_exceeded', 'r', 'a', metric='unknown', limit=0, observed=0),
            Event('budget_exceeded', 'r', 'a', metric='max_state_visits_per_run', limit=2, observed=1),
            Event('budget_exceeded', 'r', 'a', metric='max_state_visits_per_run', limit=-1, observed=0),
            Event('budget_exceeded', 'r', 'a', metric='max_state_visits_per_run', limit=0.0, observed=0),
            Event('budget_exceeded', 'r', 'a', metric='max_state_visits_per_run', limit=0, observed=False),
            Event('run_finished', 'r'), Event('run_error', 'r'),
        ]
        for field in ('kind', 'run_id', 'state', 'state_visit_id', 'invocation_id', 'outcome', 'target', 'trigger_event', 'metric'):
            invalid.append(replace(Event('run_started', 'r'), **{field: []}))
        for event in invalid:
            with self.subTest(event=event):
                self.path.write_text(json.dumps(asdict(event)) + '\n')
                with self.assertRaises(ObservabilityError):
                    tuple(read_events(self.path))
                with self.assertRaises(ObservabilityError):
                    summarize_runs([event])
        with self.assertRaisesRegex(ObservabilityError, 'expected canonical Event'):
            summarize_runs([asdict(Event('run_started', 'r'))])

    def test_inconsistent_streams_fail_explicitly(self):
        events = list(self.cycle().events)
        terminal = list(self.terminal().events)
        bad = [events[1:], [events[0], events[0]], terminal + [terminal[-1]],
               terminal + [terminal[1]], [terminal[0], terminal[-1]],
               [events[0], events[1], events[3]],  # outcome without start
               [events[0], events[1], events[2], events[2]],
               events[:4] + [events[3]],  # repeated outcome
               events[:2] + [events[1]],  # overlapping visit
               events[:3] + [replace(events[3], state='wrong')],
               events[:3] + [replace(events[3], invocation_id='wrong')],
               events[:3] + [replace(events[3], state_visit_id='wrong')],
               events[:2] + [events[4]],  # transition without outcome
               events[:4] + [replace(events[4], outcome='failed' if events[4].outcome != 'failed' else 'success')],
               events[:5] + [events[4]],  # repeated transition
               events[:3] + [events[5]],  # exit before outcome/transition
               [events[0], next(e for e in events if e.trigger_event == 'budget_exceeded')],
               events[:3] + [replace(events[3], run_id='other')],
               terminal[:-1] + [replace(terminal[-1], state='other')],
               events[:6] + [replace(events[6], state_visit_id=events[1].state_visit_id)],
               events[:7] + [replace(events[7], invocation_id=events[2].invocation_id)],
               events[:6] + [Event('run_finished', events[0].run_id, 'check')],
               ]
        for stream in bad:
            with self.subTest(stream=stream):
                with self.assertRaisesRegex(ObservabilityError, '^event '):
                    summarize_runs(stream)
                self.path.write_text(''.join(json.dumps(asdict(e)) + '\n' for e in stream))
                with self.assertRaises(ObservabilityError):
                    summarize_runs(read_events(self.path))

    def test_summary_is_deeply_read_only_and_deterministic(self):
        events = self.cycle().events
        first, = summarize_runs(events)
        self.assertEqual((first,), summarize_runs(iter(events)))
        with self.assertRaises(FrozenInstanceError):
            first.terminal_state = 'changed'
        for counts in (first.state_visit_counts, first.invocation_counts_by_state, first.outcome_counts):
            with self.assertRaises(TypeError):
                counts['changed'] = 1
        source = {'a': 1}
        summary = RunSummary('r', None, source, 1, source, source, 0, 0, False)
        source['a'] = 99
        for counts in (summary.state_visit_counts, summary.invocation_counts_by_state, summary.outcome_counts):
            self.assertEqual(dict(counts), {'a': 1})

    def test_file_io_errors_remain_os_errors(self):
        with self.assertRaises(FileNotFoundError):
            tuple(read_events(self.path))

    def test_closed_terminal_cannot_continue_execution(self):
        events = self.terminal().events
        for event in (Event('state_entered', events[0].run_id, 'other', 'new-visit'),
                      Event('run_error', events[0].run_id, 'other')):
            with self.assertRaisesRegex(ObservabilityError, 'expected completion'):
                summarize_runs(events[:-1] + (event,))

    def test_previsit_diagnostic_failure_remains_a_valid_error_history(self):
        from runtime import DiagnosticPayload

        class DiagnosticExecutor:
            def run(self, request):
                return InvocationResponse(ExecutorResult('failed'), DiagnosticPayload(b'too long'))

        flow = Flow('a', {
            'a': State('x', {'failed': 'b'}),
            'b': State('x', {'failed': 'done'}, diagnostics_from_previous_max_bytes=0),
            'done': State(terminal=True)})
        with self.assertRaises(ExecutionError) as caught:
            run(WorkUnit('t'), flow, {'x': DiagnosticExecutor()})
        summary, = summarize_runs(caught.exception.events)
        self.assertEqual(dict(summary.state_visit_counts), {'a': 1})
        self.assertEqual(summary.transition_count, 1)
        self.assertFalse(summary.completed_normally)
        self.assertIsNone(summary.terminal_state)
