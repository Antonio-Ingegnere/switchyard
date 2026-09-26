from dataclasses import FrozenInstanceError, asdict, replace
from fractions import Fraction
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from examples.delivery.acceptance import AcceptanceApproval
from examples.delivery.presets import build_accept, build_change
from examples.delivery.analytics import (DeliveryAnalyticsError, Ratio,
    summarize_delivery_analytics as analytics, summarize_delivery_lifecycles as lifecycles)
from runtime import (Event, ExecutionError, ExecutorResult, Flow, InvocationResponse,
                     RunContext, State, WorkUnit, run)
from runtime.recording import JsonlRecorder
from switchyard.observability import ObservabilityError, read_events


class Sequence:
    def __init__(self, values):
        self.values = iter(values)

    def run(self, request):
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return InvocationResponse(ExecutorResult(value))


class DeliveryAnalyticsTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def events(self, workflow='deliver', rid='a', corr='c', parent=None, verify=('success',), **outcomes):
        if workflow == 'accept':
            loaded = build_accept(workspace=str(self.root), git_executable='/fake/git',
                approval=AcceptanceApproval('a'*40, 'b'*64, 'reviewed', True))
        else:
            loaded = build_change(workflow, workspace=str(self.root), codex_executable='/fake/codex',
                                  verifier_executable='/fake/verifier', verifier_argv=())
        executors = {key: Sequence(outcomes.get(key, verify if key == 'verifier' else ('success',)))
                     for key in loaded.executors}
        samples = [(10000-i*100, 100+i*10) for i in range(100)]
        with patch('runtime.uuid4', return_value=rid), patch('runtime._Clock.sample', side_effect=samples):
            try:
                return run(WorkUnit('t'), loaded.flow, executors, context=RunContext(corr, parent)).events
            except ExecutionError as exc:
                return exc.events

    def through(self, events, kind, state):
        index = next(i for i, e in enumerate(events) if e.kind == kind and e.state == state)
        return events[:index+1]

    def test_first_pass_deliver_and_pending_manual_qa(self):
        result = analytics(self.events())
        facts, = result.runs
        self.assertEqual(facts.workflow, 'deliver')
        self.assertTrue(facts.first_pass_verification)
        self.assertFalse(facts.automatic_repair_used)
        self.assertEqual(result.first_pass_verification_rate, Ratio(1, 1))
        self.assertEqual(result.first_pass_verification_rate.value, Fraction(1))
        self.assertEqual(result.automatic_repair_rate.value, Fraction(0))
        self.assertIsNone(result.automatic_repair_success_rate.value)
        self.assertEqual(result.qa_decision_count, 0)
        self.assertIsNone(result.manual_qa_pass_rate.value)
        self.assertFalse(result.lifecycles[0].accepted)
        self.assertTrue(result.lifecycles[0].incomplete)

    def test_automatic_repair_success_is_not_first_pass_or_manual_reopen(self):
        result = analytics(self.events(verify=('failed', 'success')))
        facts, = result.runs
        self.assertFalse(facts.first_pass_verification)
        self.assertTrue(facts.automatic_repair_used)
        self.assertTrue(facts.repair_success)
        self.assertEqual(result.first_pass_verification_rate, Ratio(0, 1))
        self.assertEqual(result.automatic_repair_rate, Ratio(1, 1))
        self.assertEqual(result.automatic_repair_success_rate, Ratio(1, 1))
        self.assertEqual(result.manual_qa_reopen_count, 0)
        self.assertEqual(result.lifecycles[0].automatic_repair_count, 1)

    def test_second_failures_keep_rediagnosis_and_needs_human_separate(self):
        result = analytics(self.events(verify=('failed', 'failed')) +
                           self.events('repair', 'b', 'other', verify=('failed', 'failed')))
        self.assertEqual(result.rediagnosis_rate, Ratio(1, 1))
        self.assertEqual(result.needs_human_rate, Ratio(1, 1))
        self.assertEqual(result.automatic_repair_success_rate, Ratio(0, 2))
        self.assertEqual(result.automatic_repair_count, 2)
        self.assertEqual([l.final_terminal for l in result.lifecycles], ['NEEDS_REDIAGNOSIS', 'NEEDS_HUMAN'])
        self.assertFalse(result.lifecycles[0].incomplete)  # known failure, not pending QA
        self.assertTrue(result.lifecycles[1].incomplete)  # repair-only observed root

    def test_manual_qa_pass_and_accepted_lifecycle(self):
        result = analytics(self.events() + self.events('accept', 'b', parent='run:a'))
        lifecycle, = result.lifecycles
        self.assertEqual(lifecycle.run_ids, ('run:a', 'run:b'))
        self.assertEqual(lifecycle.final_terminal, 'ACCEPTED')
        self.assertTrue(lifecycle.accepted)
        self.assertFalse(lifecycle.incomplete)
        self.assertEqual(result.manual_qa_pass_rate, Ratio(1, 1))
        self.assertEqual(result.manual_qa_reopen_rate, Ratio(0, 1))
        self.assertEqual(result.accepted_lifecycle_count, 1)
        self.assertEqual(result.total_invocations_for_accepted_lifecycles, 6)
        self.assertEqual(result.mean_invocations_per_accepted_lifecycle, Fraction(6))
        self.assertEqual(lifecycle.automation_duration_ns, 360)

    def test_reopen_repair_accept_parent_first_despite_reverse_input(self):
        a = self.events()
        b = self.events('repair', 'b', parent='run:a')
        c = self.events('accept', 'c', parent='run:b')
        result = analytics(c + b + a)
        lifecycle, = result.lifecycles
        self.assertEqual(lifecycle.run_ids, ('run:a', 'run:b', 'run:c'))
        self.assertEqual((lifecycle.deliver_run_count, lifecycle.repair_run_count, lifecycle.accept_run_count), (1, 1, 1))
        self.assertEqual(result.qa_decision_count, 2)
        self.assertEqual(result.manual_qa_reopen_rate.value, Fraction(1, 2))
        self.assertEqual(result.manual_qa_pass_rate.value, Fraction(1, 2))
        self.assertEqual(lifecycle.total_invocation_count, 9)
        self.assertEqual(lifecycle.automation_duration_ns, 540)
        self.assertFalse(lifecycle.incomplete)

    def test_commit_blocked_still_records_manual_qa_decision(self):
        result = analytics(self.events() + self.events('accept', 'b', parent='run:a', scope=('failed',)))
        self.assertEqual(result.commit_blocked_rate, Ratio(1, 1))
        self.assertEqual(result.manual_qa_pass_count, 1)
        self.assertEqual(result.accepted_lifecycle_count, 0)
        self.assertEqual(result.first_pass_verification_rate, Ratio(1, 1))
        self.assertFalse(result.lifecycles[0].incomplete)
        self.assertIsNone(result.mean_invocations_per_accepted_lifecycle)

    def test_before_verification_is_ineligible(self):
        result = analytics(self.events(analysis=('error',)))
        self.assertFalse(result.runs[0].verification_eligible)
        self.assertIsNone(result.runs[0].first_pass_verification)
        self.assertEqual(result.eligible_verification_run_count, 0)
        self.assertIsNone(result.first_pass_verification_rate.value)
        self.assertEqual(result.rediagnosis_rate, Ratio(1, 1))

    def test_unfinished_verification_is_unknown_not_failure(self):
        events = self.through(self.events(), 'invocation_started', 'VERIFY')
        result = analytics(events)
        self.assertEqual(result.first_pass_verification_rate, Ratio(0, 1, 1))
        self.assertEqual(result.automatic_repair_rate, Ratio(0, 1, 1))
        self.assertIsNone(result.first_pass_verification_rate.value)
        self.assertEqual(result.rediagnosis_rate, Ratio(0, 1, 1))
        self.assertTrue(result.lifecycles[0].incomplete)
        self.assertIsNone(result.lifecycles[0].automation_duration_ns)
        # Recorded VERIFY success alone cannot prove the required QA-ready terminal.
        result = analytics(self.through(self.events(), 'invocation_finished', 'VERIFY'))
        self.assertIsNone(result.runs[0].first_pass_verification)

    def test_actual_repair_entry_not_failed_outcome_is_counted(self):
        events = self.events(verify=('failed', 'success'))
        before = analytics(self.through(events, 'transition_selected', 'VERIFY'))
        self.assertEqual(before.first_pass_verification_rate, Ratio(0, 1))
        self.assertEqual(before.automatic_repair_count, 0)
        self.assertEqual(before.automatic_repair_rate.unknown_count, 1)
        during = analytics(self.through(events, 'state_entered', 'REPAIR_ONCE'))
        self.assertEqual(during.automatic_repair_count, 1)
        self.assertEqual(during.automatic_repair_success_rate, Ratio(0, 1, 1))
        self.assertIsNone(during.automatic_repair_success_rate.value)

    def test_unfinished_accept_counts_qa_pass_not_acceptance(self):
        child = self.through(self.events('accept', 'b', parent='run:a'), 'state_entered', 'CHECK_WORKTREE')
        result = analytics(self.events() + child)
        self.assertEqual(result.manual_qa_pass_count, 1)
        self.assertEqual(result.commit_blocked_rate, Ratio(0, 1, 1))
        self.assertFalse(result.lifecycles[0].accepted)
        self.assertTrue(result.lifecycles[0].incomplete)

    def test_uncorrelated_runs_are_explicit_not_one_fake_lifecycle(self):
        result = analytics(self.events(corr=None) + self.events('repair', 'b', corr=None))
        self.assertEqual(result.uncorrelated_run_ids, ('run:a', 'run:b'))
        self.assertEqual(result.uncorrelated_run_count, 2)
        self.assertEqual(result.lifecycles, ())
        self.assertEqual(result.first_pass_verification_rate, Ratio(2, 2))

    def test_missing_parent_tolerated_and_incomplete(self):
        result = analytics(self.events('accept', parent='external-run'))
        self.assertTrue(result.lifecycles[0].accepted)
        self.assertTrue(result.lifecycles[0].incomplete)
        self.assertEqual(result.qa_decision_count, 0)
        self.assertEqual(result.lifecycles[0].automation_duration_ns, 180)

    def test_interleaving_and_independent_groups(self):
        a, b = self.events(), self.events(rid='b', corr='other')
        interleaved = tuple(e for pair in zip(b, a) for e in pair)
        result = analytics(interleaved)
        self.assertEqual([l.correlation_id for l in result.lifecycles], ['other', 'c'])
        self.assertEqual(result, analytics(b + a))

    def test_conflicting_correlation_rejected_but_absent_correlation_is_unknown(self):
        with self.assertRaisesRegex(DeliveryAnalyticsError, 'another correlation'):
            analytics(self.events() + self.events('repair', 'b', 'other', parent='run:a'))
        result = analytics(self.events(corr=None) + self.events('repair', 'b', parent='run:a'))
        self.assertTrue(result.lifecycles[0].incomplete)
        self.assertEqual(result.qa_decision_count, 0)

    def test_contradictory_parent_graphs_and_qa_decisions(self):
        with self.assertRaisesRegex(DeliveryAnalyticsError, 'cyclic'):
            analytics(self.events(parent='run:b') + self.events(rid='b', parent='run:a'))
        with self.assertRaisesRegex(DeliveryAnalyticsError, 'conflicting manual QA'):
            analytics(self.events() + self.events('repair', 'b', parent='run:a') +
                      self.events('accept', 'c', parent='run:a'))
        with self.assertRaisesRegex(DeliveryAnalyticsError, 'did not finish ready'):
            analytics(self.events(analysis=('failed',)) + self.events('accept', 'b', parent='run:a'))

    def test_branches_do_not_invent_final_run_or_duplicate_qa_decisions(self):
        result = analytics(self.events() + self.events('accept', 'b', parent='run:a') +
                           self.events('accept', 'c', parent='run:a'))
        lifecycle, = result.lifecycles
        self.assertEqual(result.qa_decision_count, 1)
        self.assertTrue(lifecycle.accepted)
        self.assertTrue(lifecycle.incomplete)
        self.assertIsNone(lifecycle.final_terminal)
        self.assertIsNone(result.mean_invocations_per_accepted_lifecycle)

    def generic(self, initial, target):
        return run(WorkUnit('t'), Flow(initial, {initial: State('x', {'success': target}),
                   target: State(terminal=True)}), {'x': Sequence(('success',))}).events

    def test_unrelated_and_wrong_shape_runs_are_not_reinterpreted(self):
        for events in (self.generic('OTHER', 'READY_FOR_MANUAL_QA'),
                       self.generic('ANALYZE', 'READY_FOR_MANUAL_QA'), (Event('run_started', 'r'),)):
            result = analytics(events)
            self.assertEqual(result.runs, ())
            self.assertEqual(len(result.unrecognized_run_ids), 1)
            self.assertEqual(result.lifecycles, ())
        with self.assertRaisesRegex(DeliveryAnalyticsError, 'multiple delivery workflow'):
            analytics(self.generic('ANALYZE', 'FRESH_DIAGNOSIS'))

    def test_missing_timing_never_becomes_zero(self):
        a = tuple(replace(e, occurred_at_unix_ns=None, duration_ns=None) for e in self.events())
        result = analytics(a + self.events('accept', 'b', parent='run:a'))
        self.assertIsNone(result.lifecycles[0].automation_duration_ns)
        self.assertTrue(result.lifecycles[0].accepted)
        self.assertFalse(result.lifecycles[0].incomplete)  # lifecycle shape known; timing unavailable

    def test_historical_jsonl_and_reuse_without_file_reopening(self):
        path = self.root / 'history.jsonl'
        events = self.events(corr=None)
        old = [{k: v for k, v in asdict(e).items() if k not in
                ('occurred_at_unix_ns', 'duration_ns', 'correlation_id', 'parent_run_id')} for e in events]
        path.write_text(''.join(json.dumps(record) + '\n' for record in old))
        for e in self.events(rid='b', corr='other'):
            JsonlRecorder(path).record(e)
        cached = tuple(read_events(path))
        from_file = analytics(read_events(path))
        self.assertEqual(from_file.uncorrelated_run_ids, ('run:a',))
        self.assertIsNone(from_file.runs[0].run.run_duration_ns)
        with patch.object(Path, 'open', side_effect=AssertionError('no reopening')):
            self.assertEqual(analytics(iter(cached)), from_file)
            self.assertEqual(lifecycles(cached), from_file.lifecycles)

    def test_generic_errors_remain_generic(self):
        with self.assertRaises(ObservabilityError):
            analytics(self.events()[1:])

    def test_run_error_is_observed_end_not_missing_future(self):
        result = analytics(self.events(verify=(ValueError('executor failed'),)))
        self.assertTrue(result.runs[0].ended)
        self.assertEqual(result.first_pass_verification_rate, Ratio(0, 1))
        self.assertEqual(result.rediagnosis_rate, Ratio(0, 1))
        self.assertIsNone(result.lifecycles[0].final_terminal)
        self.assertFalse(result.lifecycles[0].incomplete)

    def test_results_and_ratio_counts_are_immutable(self):
        result = analytics(self.events())
        for obj, name in ((result, 'runs'), (result.runs[0], 'workflow'),
                          (result.lifecycles[0], 'accepted'), (result.first_pass_verification_rate, 'numerator')):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, name, None)
        source = list(result.lifecycles[0].run_ids)
        copied = replace(result.lifecycles[0], run_ids=source)
        source.clear()
        self.assertEqual(copied.run_ids, ('run:a',))
        self.assertIsNone(Ratio(0, 0).value)
        self.assertIsNone(Ratio(1, 2, 1).value)
        self.assertEqual(Ratio(1, 3).value, Fraction(1, 3))
        self.assertEqual(analytics([]).lifecycle_count, 0)

    def test_accepted_history_then_reopen_is_not_final_accepted_mean(self):
        result = analytics(self.events() + self.events('accept', 'b', parent='run:a') +
                           self.events('repair', 'c', parent='run:b'))
        self.assertTrue(result.lifecycles[0].accepted)
        self.assertEqual(result.lifecycles[0].final_terminal, 'READY_FOR_MANUAL_QA')
        self.assertIsNone(result.mean_invocations_per_accepted_lifecycle)
        self.assertEqual(result.manual_qa_reopen_count, 0)  # parent was accepted, not QA-ready


    def test_unclassified_correlated_continuation_makes_final_state_unknown(self):
        completed = self.events() + self.events('accept', 'b', parent='run:a')
        pending = Event('run_started', 'run:pending', correlation_id='c', parent_run_id='run:b')
        result = analytics(completed + (pending,))
        self.assertEqual(result.unrecognized_run_ids, ('run:pending',))
        self.assertEqual(result.lifecycles[0].run_ids, ('run:a', 'run:b'))
        self.assertTrue(result.lifecycles[0].accepted)
        self.assertTrue(result.lifecycles[0].incomplete)
        self.assertIsNone(result.lifecycles[0].final_terminal)
        self.assertIsNone(result.mean_invocations_per_accepted_lifecycle)

    def test_incomplete_parent_history_does_not_invent_qa_pass(self):
        parent = self.through(self.events(), 'invocation_started', 'VERIFY')
        result = analytics(parent + self.events('accept', 'b', parent='run:a'))
        self.assertTrue(result.lifecycles[0].accepted)
        self.assertTrue(result.lifecycles[0].incomplete)
        self.assertEqual(result.qa_decision_count, 0)
        self.assertIsNone(result.lifecycles[0].automation_duration_ns)
