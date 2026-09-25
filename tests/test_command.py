from runtime import InvocationRequest, InvocationResponse
from dataclasses import FrozenInstanceError, fields, replace
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from runtime import ExecutionError, ExecutorResult, Flow, State, WorkUnit, run
from runtime.command import Command, CommandExecutor


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cwd = self.directory.name
        self.work = WorkUnit("never interpolate $TASK into argv or environment")

    def command(self, code="pass", timeout=5, args=()):
        return Command(sys.executable, ("-c", code, *args), self.cwd, timeout)

    def flow(self, outcome="timeout", target="done", limit=None):
        return Flow("command", {
            "command": State("command", {outcome: target}, max_state_visits_per_run=limit),
            "done": State(terminal=True),
            "other": State(terminal=True),
        })

    def test_real_exit_codes(self):
        for code, outcome in ((0, "success"), (7, "failed")):
            with self.subTest(code=code):
                result = CommandExecutor(self.command(f"raise SystemExit({code})")).run(InvocationRequest(self.work)).result
                self.assertEqual(result, ExecutorResult(outcome))

    def test_missing_executable_and_cwd_are_operational_errors(self):
        base = self.command()
        for command in (replace(base, executable=str(Path(self.cwd) / "absent")),
                        replace(base, cwd=str(Path(self.cwd) / "absent"))):
            with self.subTest(command=command):
                self.assertEqual(CommandExecutor(command).run(InvocationRequest(self.work)).result, ExecutorResult("error"))

    def test_literal_arguments_cwd_and_inherited_environment(self):
        literal = "spaces ; $TASK $(echo bad) * | > ${HOME}"
        code = (
            "import os,sys; from pathlib import Path; "
            "assert sys.argv[1] == " + repr(literal) + "; "
            "assert Path.cwd().samefile(sys.argv[2]); "
            "assert os.environ['RUNTIME_003_TEST_VALUE'] == 'inherited'"
        )
        with patch.dict(os.environ, {"RUNTIME_003_TEST_VALUE": "inherited"}):
            result = CommandExecutor(self.command(code, args=(literal, self.cwd))).run(InvocationRequest(self.work)).result
        self.assertEqual(result.outcome, "success")

    def test_stdin_eof_and_large_output_discarded(self):
        code = (
            "import sys; assert sys.stdin.buffer.read() == b''; "
            "data = b'x' * 65536; "
            "[(sys.stdout.buffer.write(data), sys.stderr.buffer.write(data)) for _ in range(128)]"
        )
        self.assertEqual(CommandExecutor(self.command(code)).run(InvocationRequest(self.work)).result, ExecutorResult("success"))
        self.assertEqual([f.name for f in fields(ExecutorResult)], ["outcome"])

    def test_command_ownership_and_immutability(self):
        command = self.command()
        executor = CommandExecutor(command)
        self.assertIs(executor.command, command)
        with self.assertRaises(FrozenInstanceError):
            command.argv = ()
        with self.assertRaises(FrozenInstanceError):
            executor.command = self.command()
        self.assertEqual([f.name for f in fields(WorkUnit)], ["task"])
        self.assertFalse(hasattr(executor, "next_state"))

    def test_invalid_configuration_never_launches(self):
        base = self.command()
        cases = [
            {"executable": "relative"}, {"executable": ""}, {"executable": 3},
            {"executable": sys.executable + "\0"}, {"cwd": "relative"}, {"cwd": None},
            {"argv": "one shell string"}, {"argv": ["mutable"]}, {"argv": (3,)}, {"argv": ("\0",)},
        ] + [{"invocation_timeout_seconds": value}
             for value in (0, -1, True, False, "2", float("nan"), float("inf"), 10**1000)]
        with patch("runtime.command.subprocess.Popen") as launch:
            for changes in cases:
                with self.subTest(changes=changes):
                    with self.assertRaises((TypeError, ValueError)):
                        CommandExecutor(replace(base, **changes)).run(InvocationRequest(self.work)).result
            launch.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows batch-path validation")
    def test_windows_batch_executables_rejected(self):
        for suffix in (".bat", ".CMD"):
            with self.assertRaises(ValueError):
                replace(self.command(), executable=str(Path(self.cwd) / ("script" + suffix)))

    def retained_launch(self, handles, finished_during_creation=False):
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            handles.append(process)
            if finished_during_creation:
                process.wait(timeout=5)
            process.kill = Mock(wraps=process.kill)
            process.wait = Mock(wraps=process.wait)
            return process

        return launch

    def cleanup_handles(self, handles):
        for process in handles:
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_real_timeout_kills_and_waits_before_return(self):
        handles = []
        launch = self.retained_launch(handles)
        try:
            with patch("runtime.command.subprocess.Popen", side_effect=launch):
                result = CommandExecutor(self.command("import time; time.sleep(60)", timeout=0.2)).run(InvocationRequest(self.work)).result
            self.assertEqual(result.outcome, "timeout")
            process = handles[0]
            self.assertIsNotNone(process.returncode)
            process.kill.assert_called_once_with()
            self.assertEqual(process.wait.call_args.kwargs, {})
            self.assertIsNotNone(process.poll())
        finally:
            self.cleanup_handles(handles)

    def test_deadline_expired_during_creation_running_or_already_exited(self):
        for exited in (False, True):
            with self.subTest(already_exited=exited):
                handles = []
                launch = self.retained_launch(handles, finished_during_creation=exited)
                code = "pass" if exited else "import time; time.sleep(60)"
                try:
                    # Real processes, deterministic monotonic accounting: creation
                    # crosses deadline 101 before returning control at time 102.
                    with patch("runtime.command.subprocess.Popen", side_effect=launch), \
                         patch("runtime.command.monotonic", side_effect=[100.0, 102.0]):
                        result = CommandExecutor(self.command(code, timeout=1)).run(InvocationRequest(self.work)).result
                    self.assertEqual(result.outcome, "timeout")
                    process = handles[0]
                    self.assertIsNotNone(process.returncode)
                    process.wait.assert_called_once_with()
                    self.assertEqual(process.kill.call_count, 0 if exited else 1)
                finally:
                    self.cleanup_handles(handles)

    def test_deadline_precedes_launch_and_wait_uses_remaining_time(self):
        sequence = []
        process = Mock()
        process.wait.return_value = 0

        def clock():
            sequence.append("clock")
            return 10.0 if len(sequence) == 1 else 11.5

        def launch(*args, **kwargs):
            sequence.append("launch")
            return process

        command = self.command(timeout=2)
        with patch("runtime.command.monotonic", side_effect=clock), \
             patch("runtime.command.subprocess.Popen", side_effect=launch) as popen:
            result = CommandExecutor(command).run(InvocationRequest(self.work)).result
        self.assertEqual(sequence, ["clock", "launch", "clock"])
        process.wait.assert_called_once_with(timeout=0.5)
        popen.assert_called_once_with(
            [command.executable, *command.argv], shell=False, cwd=self.cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertEqual(result.outcome, "success")

    def test_runtime_timeout_transition_and_exact_events(self):
        executor = CommandExecutor(self.command("import time; time.sleep(60)", timeout=0.1))
        for target in ("done", "other"):
            result = run(self.work, self.flow(target=target), {"command": executor})
            self.assertEqual(result.terminal_state, target)
            self.assertEqual([(e.kind, e.state, e.outcome, e.target, e.trigger_event) for e in result.events], [
                ("run_started", None, None, None, None),
                ("state_entered", "command", None, None, None),
                ("invocation_started", "command", None, None, None),
                ("invocation_finished", "command", "timeout", None, None),
                ("transition_selected", "command", "timeout", target, None),
                ("state_exited", "command", None, None, None),
                ("state_entered", target, None, None, None),
                ("state_exited", target, None, None, None),
                ("run_finished", target, None, None, None),
            ])

    def test_real_command_bounded_loop(self):
        flow = Flow("command", {
            "command": State("command", {"success": "command", "budget_exceeded": "done"}, max_state_visits_per_run=2),
            "done": State(terminal=True),
        })
        result = run(self.work, flow, {"command": CommandExecutor(self.command())})
        self.assertEqual(result.terminal_state, "done")
        self.assertEqual(sum(e.kind == "invocation_started" for e in result.events), 2)
        self.assertEqual([e.observed for e in result.events if e.kind == "budget_exceeded"], [2])

    def test_launch_oserror_is_normalized_but_internal_error_escapes(self):
        executor = CommandExecutor(self.command())
        with patch("runtime.command.subprocess.Popen", side_effect=PermissionError("denied")) as launch:
            self.assertEqual(executor.run(InvocationRequest(self.work)).result.outcome, "error")
            launch.assert_called_once()
        with patch("runtime.command.subprocess.Popen", side_effect=RuntimeError("bug")) as launch:
            with self.assertRaises(ExecutionError) as caught:
                run(self.work, self.flow(outcome="error"), {"command": executor})
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertFalse(any(e.kind == "transition_selected" for e in caught.exception.events))
            launch.assert_called_once()

    def test_unexpected_wait_error_cleans_up_and_escapes(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [OSError("wait defect"), -9]
        with patch("runtime.command.subprocess.Popen", return_value=process):
            with self.assertRaises(ExecutionError) as caught:
                run(self.work, self.flow(outcome="error"), {"command": CommandExecutor(self.command())})
        self.assertIsInstance(caught.exception.__cause__, OSError)
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(process.wait.call_args.kwargs, {})

    def test_cleanup_failure_never_returns_timeout(self):
        for failure in ("kill", "wait"):
            with self.subTest(failure=failure):
                process = Mock()
                process.poll.return_value = None
                if failure == "kill":
                    process.kill.side_effect = OSError("kill failed")
                else:
                    process.wait.side_effect = OSError("reap failed")
                with patch("runtime.command.subprocess.Popen", return_value=process), \
                     patch("runtime.command.monotonic", side_effect=[0, 10]):
                    with self.assertRaises(ExecutionError) as caught:
                        run(self.work, self.flow(), {"command": CommandExecutor(self.command(timeout=1))})
                self.assertIsInstance(caught.exception.__cause__, OSError)
                process.kill.assert_called_once_with()
                self.assertFalse(any(e.kind in ("invocation_finished", "transition_selected") for e in caught.exception.events))

    def test_exit_race_during_timeout_cleanup(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("command", 1), 0]
        process.poll.return_value = None
        process.kill.side_effect = ProcessLookupError("already exited")
        with patch("runtime.command.subprocess.Popen", return_value=process):
            result = CommandExecutor(self.command()).run(InvocationRequest(self.work)).result
        self.assertEqual(result.outcome, "timeout")
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(process.wait.call_args.kwargs, {})


if __name__ == "__main__":
    unittest.main()
