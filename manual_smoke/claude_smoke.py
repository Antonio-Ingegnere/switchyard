"""Manual Claude smoke; failure prints a diagnostic rerun, never runs it."""

import argparse
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import ExecutionError, Flow, State, WorkUnit, run
from runtime.claude import ClaudeConfig, ClaudeExecutor
from runtime.command import Command, CommandExecutor


TASK = "Create only result.txt containing exactly runtime-006-ok followed by one newline."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real", action="store_true", required=True)
    parser.add_argument("--claude-executable", required=True)
    args = parser.parse_args()
    if not Path(args.claude_executable).is_absolute():
        parser.error("--claude-executable must be absolute")
    workspace = tempfile.mkdtemp(prefix="runtime006-smoke-")
    argv = [args.claude_executable, "--print", "--no-session-persistence",
            "--output-format", "text", "--restricted", "--permission-mode", "acceptEdits",
            "--permission-prompts", "none", "--tools", "Read,Edit,Write,Glob,Grep", "--disallowedTools", "mcp__*",
            "--", "WorkUnit task:\n" + TASK]
    accepted = False
    try:
        subprocess.run([args.claude_executable, "--version"], timeout=5, check=True)
        print("Platform:", platform.platform())
        flow = Flow("implement", {
            "implement": State("claude", {"success": "verify", "error": "failed", "timeout": "failed"}),
            "verify": State("verify", {"success": "accepted", "failed": "failed", "error": "failed", "timeout": "failed"}),
            "accepted": State(terminal=True), "failed": State(terminal=True),
        })
        verifier = CommandExecutor(Command(sys.executable, ("-c",
            "from pathlib import Path; import sys; p=Path('result.txt'); "
            "sys.exit(0 if p.is_file() and p.read_bytes()==b'runtime-006-ok\\n' else 1)"), workspace, 5))
        result = run(WorkUnit(TASK), flow, {
            "claude": ClaudeExecutor(ClaudeConfig(args.claude_executable, workspace, 120)),
            "verify": verifier,
        })
        for event in result.events:
            if event.kind == "invocation_finished":
                print(event.state, event.outcome)
        accepted = result.terminal_state == "accepted"
        print("Terminal:", result.terminal_state)
    except Exception as exc:
        # Do not print arbitrary exception payloads that might contain secrets.
        print("Smoke error type:", type(exc).__name__)
        if isinstance(exc, ExecutionError) and exc.__cause__ is not None:
            print("Cause type:", type(exc.__cause__).__name__)
    finally:
        print("Smoke:", "PASS" if accepted else "FAIL")
        if accepted:
            shutil.rmtree(workspace)
        else:
            print("Workspace retained:", workspace)
            print("Diagnostic argv:", json.dumps(argv, ensure_ascii=False))
            print("Manual diagnostic only; extra model call, not smoke acceptance:")
            print("cd " + shlex.quote(workspace))
            print(shlex.join(argv))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
