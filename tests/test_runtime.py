from runtime import InvocationRequest, InvocationResponse
import unittest
from dataclasses import FrozenInstanceError, fields

from runtime import ExecutionError, ExecutorResult, Flow, FlowValidationError, State, WorkUnit, run


class FakeExecutor:
    def __init__(self, result=ExecutorResult("success")):
        self.result = result
        self.calls = []

    def run(self, work_unit):
        self.calls.append(work_unit.work_unit)
        return InvocationResponse(self.result)


class RuntimeTests(unittest.TestCase):
    def simple_flow(self, target="done", outcome="success"):
        return Flow("work", {
            "work": State("fake", {outcome: target}),
            "done": State(terminal=True),
            "other": State(terminal=True),
        })

    def test_vertical_path(self):
        work = WorkUnit("small task")
        executor = FakeExecutor()
        flow = Flow("work", {
            "work": State("fake", {"success": "done"}),
            "done": State(terminal=True),
        })
        result = run(work, flow, {"fake": executor})
        self.assertEqual(result.terminal_state, "done")
        self.assertEqual(executor.calls, [work])
        self.assertEqual([e.target for e in result.events if e.kind == "transition_selected"], ["done"])

    def test_invalid_flows_never_call_executor(self):
        done = State(terminal=True)
        work = State("fake", {"success": "done"})
        cases = {
            "unknown initial": Flow("absent", {"work": work, "done": done}),
            "null initial": Flow(None, {"done": done}),
            "empty graph": Flow("work", {}),
            "unknown target": Flow("work", {"work": State("fake", {"success": "absent"}), "done": done}),
            "invalid target type": Flow("work", {"work": State("fake", {"success": []}), "done": done}),
            "unknown executor": Flow("work", {"work": State("absent"), "done": done}),
            "missing executor": Flow("work", {"work": State(), "done": done}),
            "noncallable executor": Flow("work", {"work": State("invalid"), "done": done}),
            "terminal executor": Flow("done", {"done": State("fake", terminal=True)}),
            "terminal transition": Flow("done", {"done": State(on={"success": "done"}, terminal=True)}),
            "invalid state": Flow("work", {"work": work, "done": done, "bad": object()}),
            "invalid terminal flag": Flow("work", {"work": work, "done": State(terminal=1)}),
            "empty state name": Flow("work", {"work": work, "done": done, "": done}),
            "no terminal": Flow("work", {"work": State("fake")}),
            "runtime event as outcome": Flow("work", {"work": State("fake", {"budget_exceeded": "done"}), "done": done}),
            "unreachable invalid reference": Flow("done", {"done": done, "bad": State("fake", {"success": "absent"})}),
            "self loop": Flow("work", {"work": State("fake", {"success": "work"}), "done": done}),
            "two state cycle": Flow("work", {"work": State("fake", {"success": "again"}), "again": State("fake", {"success": "work"}), "done": done}),
            "unreachable cycle": Flow("done", {"done": done, "a": State("fake", {"success": "b"}), "b": State("fake", {"success": "a"})}),
        }
        for label, flow in cases.items():
            with self.subTest(label=label):
                executor = FakeExecutor()
                with self.assertRaises(FlowValidationError):
                    run(WorkUnit("task"), flow, {"fake": executor, "invalid": object()})
                self.assertEqual(executor.calls, [])

    def test_initial_is_required(self):
        executor = FakeExecutor()
        with self.assertRaises(TypeError):
            flow = Flow(states={"done": State(terminal=True)})
            run(WorkUnit("task"), flow, {"fake": executor})
        self.assertEqual(executor.calls, [])

    def test_only_flow_transition_changes_terminal(self):
        executor = FakeExecutor()
        for target in ("done", "other"):
            result = run(WorkUnit("task"), self.simple_flow(target), {"fake": executor})
            self.assertEqual(result.terminal_state, target)
            transitions = [e for e in result.events if e.kind == "transition_selected"]
            self.assertEqual([(e.outcome, e.target) for e in transitions], [("success", target)])
        self.assertEqual(len(executor.calls), 2)

    def test_each_normalized_outcome_is_mapped_by_runtime(self):
        for outcome in ("success", "failed", "error", "timeout", "cancelled"):
            with self.subTest(outcome=outcome):
                executor = FakeExecutor(ExecutorResult(outcome))
                result = run(WorkUnit("task"), self.simple_flow(outcome=outcome), {"fake": executor})
                self.assertEqual(result.terminal_state, "done")
                self.assertEqual(len(executor.calls), 1)

    def test_result_contract_has_no_transition_authority(self):
        self.assertEqual([f.name for f in fields(ExecutorResult)], ["outcome"])
        with self.assertRaises(TypeError):
            ExecutorResult("success", next_state="other")

    def test_malformed_results_fail_without_transition_or_retry(self):
        class ExtendedResult(ExecutorResult):
            next_state = "other"

        for bad in (None, {"outcome": "success", "next_state": "other"},
                    ExecutorResult("budget_exceeded"), ExecutorResult("passed"),
                    ExecutorResult([]), ExtendedResult("success")):
            with self.subTest(result=bad):
                executor = FakeExecutor(bad)
                with self.assertRaisesRegex(ExecutionError, "malformed") as caught:
                    run(WorkUnit("task"), self.simple_flow(), {"fake": executor})
                self.assertEqual(len(executor.calls), 1)
                self.assertFalse(any(e.kind == "transition_selected" for e in caught.exception.events))
                self.assertEqual(caught.exception.events[-1].kind, "run_error")

    def test_missing_transition_is_explicit_error(self):
        executor = FakeExecutor(ExecutorResult("failed"))
        with self.assertRaisesRegex(ExecutionError, "missing transition: work.failed") as caught:
            run(WorkUnit("task"), self.simple_flow(), {"fake": executor})
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual([e.kind for e in caught.exception.events], [
            "run_started", "state_entered", "invocation_started", "invocation_finished", "run_error",
        ])

    def test_terminal_initial_never_invokes_executor(self):
        executor = FakeExecutor()
        result = run(WorkUnit("task"), Flow("done", {"done": State(terminal=True)}), {"fake": executor})
        self.assertEqual(result.terminal_state, "done")
        self.assertEqual(executor.calls, [])
        self.assertTrue(all(e.invocation_id is None for e in result.events))
        self.assertEqual([e.kind for e in result.events], ["run_started", "state_entered", "state_exited", "run_finished"])

    def test_state_order_has_no_semantics(self):
        flow = self.simple_flow()
        reversed_flow = Flow(flow.initial, dict(reversed(list(flow.states.items()))))
        for candidate in (flow, reversed_flow):
            result = run(WorkUnit("task"), candidate, {"fake": FakeExecutor()})
            self.assertEqual(result.terminal_state, "done")
            self.assertEqual([e.state for e in result.events if e.kind == "state_entered"], ["work", "done"])

    def test_work_unit_is_immutable_and_delivered_to_each_invocation(self):
        work = WorkUnit("original")
        with self.assertRaises(FrozenInstanceError):
            work.task = "changed"
        with self.assertRaises(TypeError):
            WorkUnit(["mutable"])
        executor = FakeExecutor()
        flow = Flow("a", {"a": State("fake", {"success": "b"}), "b": State("fake", {"success": "done"}), "done": State(terminal=True)})
        run(work, flow, {"fake": executor})
        self.assertEqual(len(executor.calls), 2)
        self.assertTrue(all(payload is work for payload in executor.calls))

    def test_flow_copies_and_freezes_input_mappings(self):
        transitions = {"success": "done"}
        states = {"work": State("fake", transitions), "done": State(terminal=True)}
        flow = Flow("work", states)
        transitions["success"] = "absent"
        states.clear()
        with self.assertRaises(TypeError):
            flow.states["work"].on["success"] = "absent"
        self.assertEqual(run(WorkUnit("task"), flow, {"fake": FakeExecutor()}).terminal_state, "done")

    def test_events_correlate_distinct_runs_visits_and_invocations(self):
        flow = Flow("a", {"a": State("fake", {"success": "b"}), "b": State("fake", {"success": "done"}), "done": State(terminal=True)})
        identities = set()
        for _ in range(2):
            result = run(WorkUnit("task"), flow, {"fake": FakeExecutor()})
            run_ids = {e.run_id for e in result.events}
            visits = [e for e in result.events if e.kind == "state_entered"]
            invocations = [e for e in result.events if e.kind == "invocation_started"]
            visit_ids = {e.state_visit_id for e in visits}
            invocation_ids = {e.invocation_id for e in invocations}
            self.assertEqual((len(run_ids), len(visit_ids), len(invocation_ids)), (1, 3, 2))
            current_ids = run_ids | visit_ids | invocation_ids
            self.assertEqual(len(current_ids), 6)
            self.assertTrue(identities.isdisjoint(current_ids))
            identities.update(current_ids)
            for invocation in invocations:
                related = [e for e in result.events if e.invocation_id == invocation.invocation_id]
                self.assertEqual([e.kind for e in related], ["invocation_started", "invocation_finished", "transition_selected"])
                self.assertTrue(all(e.state_visit_id == invocation.state_visit_id and e.state == invocation.state for e in related))
                self.assertIn(invocation.state_visit_id, visit_ids)
            self.assertTrue(all(e.invocation_id is None for e in result.events if e.state == "done"))
            self.assertEqual(result.events[0].kind, "run_started")
            self.assertEqual(result.events[-1].kind, "run_finished")

    def test_unexpected_executor_exception_is_not_an_implicit_transition(self):
        class BrokenExecutor:
            def run(self, work_unit):
                raise ValueError("broken")

        with self.assertRaisesRegex(ExecutionError, "executor raised") as caught:
            run(WorkUnit("task"), self.simple_flow(outcome="error"), {"fake": BrokenExecutor()})
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertEqual(caught.exception.events[-1].kind, "run_error")
        self.assertFalse(any(e.kind == "transition_selected" for e in caught.exception.events))


if __name__ == "__main__":
    unittest.main()
