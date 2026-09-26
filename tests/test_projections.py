from dataclasses import FrozenInstanceError, asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from runtime import Event
from runtime.recording import JsonlRecorder
from switchyard import observability
from switchyard.observability import (BudgetSummary, ObservabilityError, RunSummary,
                                     project_runs, read_events, summarize_budgets, summarize_runs)


def history(run_id='r', state='work'):
    return (
        Event('run_started', run_id),
        Event('state_entered', run_id, state, 'v1'),
        Event('invocation_started', run_id, state, 'v1', 'i1'),
        Event('invocation_finished', run_id, state, 'v1', 'i1', 'failed'),
        Event('transition_selected', run_id, state, 'v1', 'i1', 'failed', 'done'),
        Event('state_exited', run_id, state, 'v1'),
        Event('state_entered', run_id, 'done', 'v2'),
        Event('state_exited', run_id, 'done', 'v2'),
        Event('run_finished', run_id, 'done'),
    )


def budget_history(run_id='r', state='work'):
    return (
        Event('run_started', run_id),
        Event('budget_exceeded', run_id, state, metric='max_state_visits_per_run', limit=0, observed=0),
        Event('transition_selected', run_id, state, target='done', trigger_event='budget_exceeded'),
        Event('state_entered', run_id, 'done', 'v1'),
        Event('state_exited', run_id, 'done', 'v1'),
        Event('run_finished', run_id, 'done'),
    )


class ProjectionTests(unittest.TestCase):
    def test_one_run_callback_receives_immutable_history(self):
        events = history()
        received = []
        def projector(run_events):
            self.assertIs(type(run_events), tuple)
            received.append(run_events)
            with self.assertRaises(TypeError):
                run_events[0] = events[1]
            with self.assertRaises(FrozenInstanceError):
                run_events[0].run_id = 'other'
            return tuple(event.kind for event in run_events)
        self.assertEqual(project_runs(iter(events), projector), (tuple(e.kind for e in events),))
        self.assertEqual(received, [events])

    def test_multiple_and_interleaved_runs_preserve_first_seen_order(self):
        a, b = history('a'), history('b')
        interleaved = tuple(e for pair in zip(b, a) for e in pair)
        self.assertEqual(project_runs(a + b, tuple), (a, b))
        self.assertEqual(project_runs(interleaved, tuple), (b, a))
        self.assertEqual(project_runs(interleaved, lambda events: events[0].run_id), ('b', 'a'))

    def test_incomplete_and_empty_histories(self):
        events = history()
        for length in range(1, len(events)):
            with self.subTest(length=length):
                self.assertEqual(project_runs(iter(events[:length]), tuple), (events[:length],))
                self.assertFalse(summarize_runs(events[:length])[0].completed_normally)
        projector = Mock()
        self.assertEqual(project_runs([], projector), ())
        self.assertEqual(summarize_budgets([]), ())
        projector.assert_not_called()

    def test_shared_validation_once_before_callbacks(self):
        events = history()
        with patch.object(observability, '_validate', wraps=observability._validate) as validate:
            self.assertEqual(project_runs(events, len), (len(events),))
            self.assertEqual([call.args[0] for call in validate.call_args_list], list(events))
        invalid = (Event('state_entered', 'bad', 'x', 'v'),)
        projector = Mock()
        with self.assertRaisesRegex(ObservabilityError, 'event 10: run must begin'):
            project_runs(events + invalid, projector)
        projector.assert_not_called()

    def test_both_builtins_reject_the_same_inconsistent_history(self):
        events = history()
        malformed = [events[1:], events + (events[-1],),
                     (events[0], events[0]), events[:2] + (events[3],),
                     events[:2] + (replace(events[2], correlation_id='changed'),)]
        for stream in malformed:
            messages = []
            for function in (summarize_runs, summarize_budgets):
                with self.assertRaises(ObservabilityError) as caught:
                    function(stream)
                messages.append(str(caught.exception))
            self.assertEqual(messages[0], messages[1])

    def test_callback_programming_errors_are_not_relabelled(self):
        for error in (TypeError('bug'), RuntimeError('bug'), ObservabilityError('callback error')):
            with self.subTest(error=error), self.assertRaises(type(error)) as caught:
                project_runs(history(), Mock(side_effect=error))
            self.assertIs(caught.exception, error)

    def test_run_summary_preserves_obs004_values(self):
        durations = [None, None, None, 10, None, 40, None, 10, 80]
        events = tuple(replace(e, occurred_at_unix_ns=1000+i*100, duration_ns=durations[i],
                               correlation_id='c', parent_run_id='external') for i, e in enumerate(history()))
        expected = RunSummary('r', 'done', {'work': 1, 'done': 1}, 1, {'work': 1},
                              {'failed': 1}, 1, 0, True, 80, 10, {'work': 40, 'done': 10}, 'c', 'external')
        self.assertEqual(summarize_runs(events), (expected,))
        incomplete = replace(expected, terminal_state=None, completed_normally=False,
                             run_duration_ns=None, state_visit_counts={'work': 1},
                             state_duration_ns={'work': None}, transition_count=0)
        self.assertEqual(summarize_runs(events[:4]), (incomplete,))

    def test_budget_facts_are_deterministic_and_names_are_opaque(self):
        for name in ('repair', 'accepted', 'ready_for_manual_qa', 'arbitrary'):
            with self.subTest(name=name):
                events = budget_history(state=name)
                expected = BudgetSummary('r', 1, {name: 1})
                self.assertEqual(summarize_budgets(iter(events)), (expected,))
                self.assertEqual(summarize_budgets(events), (expected,))
                self.assertEqual(summarize_budgets(events[:2]), (expected,))
                self.assertEqual(summarize_budgets(history(state=name)), (BudgetSummary('r', 0, {}),))

    def test_budget_results_are_immutable_without_aliases(self):
        counts = {'x': 1}
        result = BudgetSummary('r', 1, counts)
        counts['x'] = 20
        self.assertEqual(dict(result.counts_by_state), {'x': 1})
        with self.assertRaises(FrozenInstanceError):
            result.run_id = 'new'
        with self.assertRaises(TypeError):
            result.counts_by_state['x'] = 2

    def test_jsonl_independent_runs_and_sequence_reuse_without_rereading(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'events.jsonl'
            recorder = JsonlRecorder(path)
            events = budget_history('budget') + history('ordinary')
            for event in events:
                recorder.record(event)
            expected = (BudgetSummary('budget', 1, {'work': 1}), BudgetSummary('ordinary', 0, {}))
            self.assertEqual(summarize_budgets(read_events(path)), expected)
            cached = tuple(read_events(path))
            with patch.object(Path, 'open', side_effect=AssertionError('must not reread')):
                self.assertEqual(summarize_budgets(cached), expected)
                runs = summarize_runs(cached)
                self.assertEqual([r.run_id for r in runs], ['budget', 'ordinary'])
                self.assertEqual([r.budget_exceeded_count for r in runs], [1, 0])
                self.assertEqual(project_runs(cached, lambda events: events[0].run_id), ('budget', 'ordinary'))

    def test_historical_schema_unavailable_values_and_strict_reader(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.jsonl'
            optional = {'occurred_at_unix_ns', 'duration_ns', 'correlation_id', 'parent_run_id'}
            records = [{k: v for k, v in asdict(e).items() if k not in optional} for e in budget_history()]
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            events = tuple(read_events(path))
            summary, = summarize_runs(events)
            self.assertIsNone(summary.run_duration_ns)
            self.assertIsNone(summary.invocation_duration_ns_total)
            self.assertEqual(dict(summary.state_duration_ns), {'done': None})
            self.assertIsNone(summary.correlation_id)
            self.assertIsNone(summary.parent_run_id)
            self.assertEqual(summarize_budgets(events), (BudgetSummary('r', 1, {'work': 1}),))
            records[-1]['unsupported'] = 1
            path.write_text(''.join(json.dumps(r) + '\n' for r in records))
            callback = Mock()
            with self.assertRaises(ObservabilityError):
                project_runs(read_events(path), callback)
            callback.assert_not_called()
