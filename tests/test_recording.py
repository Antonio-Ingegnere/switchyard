from runtime import InvocationRequest, InvocationResponse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from runtime import Event, ExecutionError, ExecutorResult, Flow, FlowValidationError, State, WorkUnit, run
from runtime.command import Command, CommandExecutor
from runtime.recording import JsonlRecorder
from test_runtime import FakeExecutor


class RecordingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "events.jsonl"
        self.work = WorkUnit("task")

    def flow(self, bounded=False):
        if bounded:
            return Flow("a", {
                "a": State("fake", {"success": "a", "budget_exceeded": "done"}, max_state_visits_per_run=2),
                "done": State(terminal=True),
            })
        return Flow("a", {
            "a": State("fake", {"success": "b"}),
            "b": State("fake", {"success": "done"}),
            "done": State(terminal=True),
        })

    def test_success_and_budget_records_match_memory_and_append(self):
        recorder = JsonlRecorder(self.path)
        expected = []
        run_ids = set()
        previous = b""
        for bounded in (False, True):
            result = run(self.work, self.flow(bounded), {"fake": FakeExecutor()}, recorder=recorder)
            expected.extend(asdict(e) for e in result.events)
            run_ids.add(result.events[0].run_id)
            data = self.path.read_bytes()
            self.assertTrue(data.startswith(previous))
            previous = data
            self.assertTrue(data.endswith(b"\n"))
            self.assertEqual([json.loads(line) for line in data.decode("utf-8").splitlines()], expected)
        self.assertEqual(len(run_ids), 2)
        transitions = [e for e in expected if e["kind"] == "transition_selected"]
        self.assertTrue(any(e["outcome"] == "success" and e["trigger_event"] is None for e in transitions))
        self.assertTrue(any(e["outcome"] is None and e["trigger_event"] == "budget_exceeded" for e in transitions))

    def test_unicode_newlines_nulls_and_stable_bytes(self):
        event = Event("state_entered", "run:1", "проверка\nстрока", "visit:1")
        recorder = JsonlRecorder(self.path)
        recorder.record(event)
        recorder.record(event)
        lines = self.path.read_bytes().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], lines[1])
        decoded = json.loads(lines[0])
        self.assertEqual(decoded, asdict(event))
        self.assertEqual(list(decoded), sorted(asdict(event)))
        self.assertIn("проверка".encode(), lines[0])
        self.assertIsNone(decoded["invocation_id"])

    def test_real_command_records_without_behavior_change(self):
        executor = CommandExecutor(Command(sys.executable, ("-c", "pass"), str(self.path.parent), 5))
        result = run(self.work, self.flow(), {"fake": executor}, recorder=JsonlRecorder(self.path))
        self.assertEqual(result.terminal_state, "done")
        self.assertEqual([json.loads(line) for line in self.path.read_text().splitlines()],
                         [asdict(e) for e in result.events])

    @patch("runtime._Clock.sample", new=lambda self: (1000, 500))
    def test_failure_at_every_event_stops_without_later_executor_or_event(self):
        for bounded in (False, True):
            with patch("runtime.uuid4", return_value="fixed"):
                baseline = run(self.work, self.flow(bounded), {"fake": FakeExecutor()})
            for position in range(len(baseline.events)):
                with self.subTest(bounded=bounded, position=position, kind=baseline.events[position].kind):
                    disk_error = OSError("disk full")
                    recorded = []

                    def record(event):
                        recorded.append(event)
                        if len(recorded) == position + 1:
                            raise disk_error

                    executor = FakeExecutor()
                    with patch("runtime.uuid4", return_value="fixed"):
                        with self.assertRaises(ExecutionError) as caught:
                            run(self.work, self.flow(bounded), {"fake": executor}, recorder=Mock(record=record))
                    error = caught.exception
                    self.assertIs(error.__cause__, disk_error)
                    self.assertIsNone(error.recorder_error)
                    self.assertEqual(error.events, baseline.events[:position + 1])
                    self.assertEqual(recorded, list(error.events))
                    self.assertEqual(len(executor.calls), sum(
                        e.kind == "invocation_started" for e in baseline.events[:position]))

    def test_executor_failure_remains_primary_when_run_error_recording_fails(self):
        adapter_bug = RuntimeError("adapter bug")
        disk_error = OSError("disk full")
        executor = Mock()
        executor.run.side_effect = adapter_bug
        recorded = []

        def record(event):
            recorded.append(event)
            if event.kind == "run_error":
                raise disk_error

        with self.assertRaisesRegex(ExecutionError, "executor raised in state a") as caught:
            run(self.work, self.flow(), {"fake": executor}, recorder=Mock(record=record))
        self.assertIs(caught.exception.__cause__, adapter_bug)
        self.assertIs(caught.exception.recorder_error, disk_error)
        self.assertEqual(tuple(recorded), caught.exception.events)
        self.assertEqual(recorded[-1].kind, "run_error")
        self.assertEqual(sum(e.kind == "run_error" for e in recorded), 1)
        executor.run.assert_called_once()

    def test_runtime_failures_remain_primary_when_run_error_recording_fails(self):
        cases = [
            (self.flow(), FakeExecutor(ExecutorResult("failed")), "missing transition: a.failed"),
            (self.flow(), FakeExecutor(None), "malformed result"),
            (Flow("a", {"a": State("fake", max_state_visits_per_run=0), "done": State(terminal=True)}),
             FakeExecutor(), "missing transition: a.budget_exceeded"),
        ]
        for flow, executor, message in cases:
            with self.subTest(message=message):
                disk_error = OSError("disk full")
                recorder = Mock()
                recorder.record.side_effect = lambda e: (_ for _ in ()).throw(disk_error) if e.kind == "run_error" else None
                with self.assertRaisesRegex(ExecutionError, message) as caught:
                    run(self.work, flow, {"fake": executor}, recorder=recorder)
                self.assertIsNone(caught.exception.__cause__)
                self.assertIs(caught.exception.recorder_error, disk_error)
                self.assertEqual(caught.exception.events[-1].kind, "run_error")
                self.assertEqual(recorder.record.call_count, len(caught.exception.events))

    def test_ordinary_run_error_is_recorded_once(self):
        with self.assertRaises(ExecutionError) as caught:
            run(self.work, self.flow(), {"fake": FakeExecutor(None)}, recorder=JsonlRecorder(self.path))
        self.assertEqual([json.loads(line) for line in self.path.read_text().splitlines()],
                         [asdict(e) for e in caught.exception.events])
        self.assertIsNone(caught.exception.recorder_error)

    def test_validation_failure_does_not_record(self):
        recorder = Mock()
        with self.assertRaises(FlowValidationError):
            run(self.work, Flow("missing", {}), {}, recorder=recorder)
        recorder.record.assert_not_called()

    def test_serialization_and_encoding_fail_before_open_and_poison_recorder(self):
        event = Event("run_started", "r")
        for bad in (replace(event, state=object()), replace(event, state="\ud800"), replace(event, limit=float("nan"))):
            with self.subTest(bad=bad):
                self.path.write_bytes(b"existing\n")
                recorder = JsonlRecorder(self.path)
                with patch("runtime.recording.Path.open") as open_file:
                    with self.assertRaises((TypeError, ValueError, UnicodeError)):
                        recorder.record(bad)
                    with self.assertRaisesRegex(RuntimeError, "unusable"):
                        recorder.record(event)
                    open_file.assert_not_called()
                self.assertEqual(self.path.read_bytes(), b"existing\n")

    def test_open_failure_is_visible_and_recorder_unusable(self):
        recorder = JsonlRecorder(self.path / "missing" / "log")
        with self.assertRaises(ExecutionError) as caught:
            run(self.work, self.flow(), {"fake": FakeExecutor()}, recorder=recorder)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual([e.kind for e in caught.exception.events], ["run_started"])
        with patch("runtime.recording.Path.open") as open_file:
            with self.assertRaisesRegex(RuntimeError, "unusable"):
                recorder.record(Event("run_started", "r"))
            open_file.assert_not_called()

    def test_lifecycle_and_write_flush_close_failures(self):
        for failure in (None, "short", "write", "flush", "close"):
            with self.subTest(failure=failure):
                operations = []

                class Stream:
                    def __enter__(self):
                        return self

                    def write(self, data):
                        operations.append("write")
                        json.loads(data)
                        if failure == "write":
                            raise OSError("write failed")
                        return 1 if failure == "short" else len(data)

                    def flush(self):
                        operations.append("flush")
                        if failure == "flush":
                            raise OSError("flush failed")

                    def __exit__(self, *args):
                        operations.append("close")
                        if failure == "close":
                            raise OSError("close failed")

                recorder = JsonlRecorder(self.path)
                with patch("runtime.recording.Path.open", return_value=Stream()) as open_file, patch("os.fsync") as fsync:
                    if failure:
                        with self.assertRaises(ExecutionError) as caught:
                            run(self.work, self.flow(), {"fake": FakeExecutor()}, recorder=recorder)
                        self.assertIsInstance(caught.exception.__cause__, OSError)
                        with self.assertRaisesRegex(RuntimeError, "unusable"):
                            recorder.record(Event("run_started", "r"))
                    else:
                        recorder.record(Event("run_started", "r"))
                    open_file.assert_called_once_with("ab")
                    fsync.assert_not_called()
                self.assertEqual(operations, ["write", "close"] if failure in ("short", "write") else ["write", "flush", "close"])

    def test_partial_write_preserves_prefix_without_retry_or_repair(self):
        recorder = JsonlRecorder(self.path)
        recorder.record(Event("run_started", "previous"))
        prefix = self.path.read_bytes()
        actual_open = Path.open

        class PartialStream:
            def __enter__(inner):
                inner.stream = actual_open(self.path, "ab")
                return inner

            def write(inner, data):
                inner.stream.write(data[:7])
                inner.stream.flush()
                raise OSError("partial write")

            def __exit__(inner, *args):
                inner.stream.close()

        with patch("runtime.recording.Path.open", return_value=PartialStream()) as open_file:
            with self.assertRaises(ExecutionError) as caught:
                run(self.work, self.flow(), {"fake": FakeExecutor()}, recorder=recorder)
            open_file.assert_called_once_with("ab")
        data = self.path.read_bytes()
        self.assertEqual(data[:len(prefix)], prefix)
        self.assertEqual(len(data) - len(prefix), 7)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(data[len(prefix):])
        self.assertEqual([e.kind for e in caught.exception.events], ["run_started"])


if __name__ == "__main__":
    unittest.main()
