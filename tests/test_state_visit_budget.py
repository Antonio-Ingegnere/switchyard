from runtime import InvocationRequest, InvocationResponse
from dataclasses import replace
import unittest

from runtime import Event, ExecutionError, ExecutorResult, Flow, FlowValidationError, State, WorkUnit, run
from test_runtime import FakeExecutor


def repair_flow(target="human"):
    return Flow("test", {
        "test": State("test", {"failed": "fix"}),
        "fix": State("fix", {"success": "test", "budget_exceeded": target}, max_state_visits_per_run=2),
        "human": State(terminal=True),
        "other": State(terminal=True),
    })


class StateVisitBudgetTests(unittest.TestCase):
    def test_exact_repair_sequence_and_refused_entry_identity(self):
        work = WorkUnit("repair")
        calls = []

        class RecordingExecutor:
            def __init__(self, name, outcome):
                self.name, self.outcome = name, outcome

            def run(self, payload):
                calls.append((self.name, payload.work_unit))
                return InvocationResponse(ExecutorResult(self.outcome))

        result = run(work, repair_flow(), {
            "test": RecordingExecutor("test", "failed"),
            "fix": RecordingExecutor("fix", "success"),
        })
        self.assertEqual(result.terminal_state, "human")
        self.assertEqual([name for name, _ in calls], ["test", "fix", "test", "fix", "test"])
        self.assertTrue(all(payload is work for _, payload in calls))
        run_id = result.events[0].run_id
        expected = [Event("run_started", run_id)]
        # Explicit accepted itinerary; every non-timing field is compared.
        for number, state, outcome, target in (
            (1, "test", "failed", "fix"),
            (2, "fix", "success", "test"),
            (3, "test", "failed", "fix"),
            (4, "fix", "success", "test"),
            (5, "test", "failed", "fix"),
        ):
            visit_id = f"{run_id}:visit:{number}"
            invocation_id = f"{run_id}:invocation:{number}"
            expected.extend([
                Event("state_entered", run_id, state, visit_id),
                Event("invocation_started", run_id, state, visit_id, invocation_id),
                Event("invocation_finished", run_id, state, visit_id, invocation_id, outcome),
                Event("transition_selected", run_id, state, visit_id, invocation_id, outcome, target),
                Event("state_exited", run_id, state, visit_id),
            ])
        expected.extend([
            Event("budget_exceeded", run_id, "fix", metric="max_state_visits_per_run", limit=2, observed=2),
            Event("transition_selected", run_id, "fix", target="human", trigger_event="budget_exceeded"),
            Event("state_entered", run_id, "human", f"{run_id}:visit:6"),
            Event("state_exited", run_id, "human", f"{run_id}:visit:6"),
            Event("run_finished", run_id, "human"),
        ])
        self.assertEqual(tuple(replace(e, occurred_at_unix_ns=None, duration_ns=None)
                               for e in result.events), tuple(expected))

    def test_zero_initial_limit_denies_first_entry(self):
        executor = FakeExecutor()
        flow = Flow("a", {
            "a": State("fake", {"budget_exceeded": "done"}, max_state_visits_per_run=0),
            "done": State(terminal=True),
        })
        result = run(WorkUnit("task"), flow, {"fake": executor})
        self.assertEqual(executor.calls, [])
        self.assertEqual([e.kind for e in result.events], [
            "run_started", "budget_exceeded", "transition_selected",
            "state_entered", "state_exited", "run_finished",
        ])
        budget = result.events[1]
        self.assertEqual((budget.limit, budget.observed), (0, 0))
        self.assertIsNone(budget.state_visit_id)
        self.assertIsNone(budget.invocation_id)
        self.assertTrue(result.events[3].state_visit_id.endswith(":visit:1"))

    def test_only_exhaustion_target_changes_terminal(self):
        executors = {"test": FakeExecutor(ExecutorResult("failed")), "fix": FakeExecutor()}
        for target in ("human", "other"):
            result = run(WorkUnit("task"), repair_flow(target), executors)
            self.assertEqual(result.terminal_state, target)
            transition = [e for e in result.events if e.trigger_event == "budget_exceeded"]
            self.assertEqual([e.target for e in transition], [target])
            self.assertIsNone(transition[0].outcome)
        self.assertEqual(len(executors["test"].calls), 6)
        self.assertEqual(len(executors["fix"].calls), 4)

    def test_missing_budget_handler_aborts_after_event(self):
        for limit in (0, 2):
            with self.subTest(limit=limit):
                executor = FakeExecutor()
                flow = Flow("a", {
                    "a": State("fake", {"success": "a"}, max_state_visits_per_run=limit),
                    "done": State(terminal=True),
                })
                with self.assertRaisesRegex(ExecutionError, "a.budget_exceeded") as caught:
                    run(WorkUnit("task"), flow, {"fake": executor})
                self.assertEqual(len(executor.calls), limit)
                tail = caught.exception.events[-2:]
                self.assertEqual([e.kind for e in tail], ["budget_exceeded", "run_error"])
                self.assertEqual(tail[0].observed, limit)
                self.assertTrue(all(e.state_visit_id is None and e.invocation_id is None for e in tail))

    def test_invalid_limits_and_handlers_prevent_all_execution(self):
        done = State(terminal=True)
        cases = [Flow("a", {"a": State("fake", max_state_visits_per_run=value), "done": done})
                 for value in (-1, True, False, 1.5, "2")]
        cases.extend([
            Flow("done", {"done": State(terminal=True, max_state_visits_per_run=0)}),
            Flow("a", {"a": State("fake", {"budget_exceeded": "done"}), "done": done}),
            Flow("a", {"a": State("fake", {"budget_exceeded": "absent"}, max_state_visits_per_run=1), "done": done}),
            Flow("a", {"a": State("fake", {"budget_exceeded": "a"}, max_state_visits_per_run=1), "done": done}),
            # Even unreachable malformed state definitions must be rejected.
            Flow("done", {"done": done, "bad": State("fake", max_state_visits_per_run=-1)}),
        ])
        for flow in cases:
            with self.subTest(flow=flow):
                executor = FakeExecutor()
                with self.assertRaises(FlowValidationError):
                    run(WorkUnit("task"), flow, {"fake": executor})
                self.assertEqual(executor.calls, [])

    def test_bounded_self_loop_is_accepted(self):
        executor = FakeExecutor()
        flow = Flow("a", {
            "a": State("fake", {"success": "a", "budget_exceeded": "done"}, max_state_visits_per_run=2),
            "done": State(terminal=True),
        })
        result = run(WorkUnit("task"), flow, {"fake": executor})
        self.assertEqual(result.terminal_state, "done")
        self.assertEqual(len(executor.calls), 2)

    def test_unbounded_subcycle_in_bounded_component_is_rejected(self):
        for initial in ("bounded", "done"):
            with self.subTest(initial=initial):
                executor = FakeExecutor()
                flow = Flow(initial, {
                    "bounded": State("fake", {"success": "a", "budget_exceeded": "done"}, max_state_visits_per_run=2),
                    "a": State("fake", {"success": "b", "failed": "bounded"}),
                    "b": State("fake", {"success": "a"}),
                    "done": State(terminal=True),
                })
                with self.assertRaisesRegex(FlowValidationError, "cycle without a bounded state"):
                    run(WorkUnit("task"), flow, {"fake": executor})
                self.assertEqual(executor.calls, [])

    def test_independent_counters_reset_across_runs(self):
        flow = Flow("a", {
            "a": State("fake", {"success": "b", "budget_exceeded": "done"}, max_state_visits_per_run=2),
            "b": State("fake", {"success": "a", "budget_exceeded": "done"}, max_state_visits_per_run=1),
            "done": State(terminal=True),
        })
        identities = set()
        for _ in range(2):
            executor = FakeExecutor()
            result = run(WorkUnit("task"), flow, {"fake": executor})
            entries = [e for e in result.events if e.kind == "state_entered"]
            self.assertEqual([e.state for e in entries], ["a", "b", "a", "done"])
            budget = [e for e in result.events if e.kind == "budget_exceeded"][0]
            self.assertEqual((budget.state, budget.limit, budget.observed), ("b", 1, 1))
            self.assertEqual(len(executor.calls), 3)
            starts = [e for e in result.events if e.kind == "invocation_started"]
            run_ids = {e.run_id for e in result.events}
            visit_ids = {e.state_visit_id for e in entries}
            invocation_ids = {e.invocation_id for e in starts}
            current = run_ids | visit_ids | invocation_ids
            self.assertEqual(len(current), 8)
            self.assertTrue(current.isdisjoint(identities))
            identities.update(current)
            for start in starts:
                related = [e for e in result.events if e.invocation_id == start.invocation_id]
                self.assertTrue(all(e.state_visit_id == start.state_visit_id and e.state == start.state for e in related))

    def test_operational_error_consumes_visit_but_escaped_exception_aborts(self):
        flow = Flow("a", {
            "a": State("fake", {"error": "a", "budget_exceeded": "done"}, max_state_visits_per_run=1),
            "done": State(terminal=True),
        })
        executor = FakeExecutor(ExecutorResult("error"))
        result = run(WorkUnit("task"), flow, {"fake": executor})
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual([e.observed for e in result.events if e.kind == "budget_exceeded"], [1])

        class BrokenExecutor:
            calls = 0

            def run(self, work_unit):
                self.calls += 1
                raise ValueError("broken")

        broken = BrokenExecutor()
        with self.assertRaises(ExecutionError) as caught:
            run(WorkUnit("task"), flow, {"fake": broken})
        self.assertEqual(broken.calls, 1)
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertFalse(any(e.kind in ("transition_selected", "budget_exceeded") for e in caught.exception.events))

    def test_executor_cannot_emit_budget_outcome_with_handler_present(self):
        executor = FakeExecutor(ExecutorResult("budget_exceeded"))
        flow = Flow("a", {
            "a": State("fake", {"budget_exceeded": "done"}, max_state_visits_per_run=1),
            "done": State(terminal=True),
        })
        with self.assertRaisesRegex(ExecutionError, "malformed") as caught:
            run(WorkUnit("task"), flow, {"fake": executor})
        self.assertEqual(len(executor.calls), 1)
        self.assertFalse(any(e.kind in ("budget_exceeded", "transition_selected") for e in caught.exception.events))


if __name__ == "__main__":
    unittest.main()
