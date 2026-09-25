"""Manual only: invokes real Codex and consumes model tokens with --run-real."""

import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import platform

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import Flow, State, WorkUnit, run
from runtime.codex import CodexConfig, CodexExecutor
from runtime.command import Command, CommandExecutor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real", action="store_true", required=True)
    parser.add_argument("--codex-executable", required=True)
    args = parser.parse_args()
    if not Path(args.codex_executable).is_absolute():
        parser.error("--codex-executable must be absolute")
    # Version probe only; do not capture or retain CLI output.
    subprocess.run([args.codex_executable, "--version"],
                   stderr=subprocess.DEVNULL, timeout=5, check=True)
    print("Platform:", platform.platform())
    with TemporaryDirectory(prefix="runtime005-smoke-") as workspace:
        Path(workspace, "AGENTS.md").write_text(
            "Create only result.txt as requested. Do not modify other files.\n", encoding="utf-8")
        flow = Flow("implement", {
            "implement": State("codex", {"success": "verify", "error": "failed", "timeout": "failed"}),
            "verify": State("verify", {"success": "accepted", "failed": "failed", "error": "failed", "timeout": "failed"}),
            "accepted": State(terminal=True), "failed": State(terminal=True),
        })
        verifier = CommandExecutor(Command(sys.executable, ("-c",
            "from pathlib import Path; import sys; p=Path('result.txt'); "
            "sys.exit(0 if p.is_file() and p.read_bytes()==b'runtime-005-ok\\n' else 1)"), workspace, 5))
        result = run(WorkUnit("Create result.txt containing exactly runtime-005-ok followed by one newline."),
                     flow, {"codex": CodexExecutor(CodexConfig(args.codex_executable, workspace, 120)),
                            "verify": verifier})
        for event in result.events:
            if event.kind == "invocation_finished":
                print(event.state, event.outcome)
        print("Smoke:", "PASS" if result.terminal_state == "accepted" else "FAIL")
        return 0 if result.terminal_state == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
