"""Manual opt-in: one runtime-scheduled Codex repair invocation, not a token budget."""

import argparse
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import (DiagnosticInput, DiagnosticOrigin, DiagnosticPayload,
                     ExecutionError, Flow, InvocationRequest, State, WorkUnit, run)
from runtime.codex import CodexConfig, CodexExecutor, _prompt
from runtime.command import Command, CommandExecutor

DIAGNOSTIC_BYTES = b'result.txt must contain exactly runtime-009-ok followed by one newline; current bytes are incorrect'
TASK = 'Make result.txt satisfy the repository verification. Modify only result.txt.'
VERIFIER_CODE = (
    "import os; from pathlib import Path; p=Path('result.txt'); "
    "ok=p.is_file() and p.read_bytes()==b'runtime-009-ok\\n'; "
    f"os.write(2, {DIAGNOSTIC_BYTES!r}) if not ok else None; "
    "raise SystemExit(0 if ok else 1)"
)


def repair_flow():
    return Flow('verify', {
        'verify': State('verify', {'success': 'accepted', 'failed': 'fix',
                                  'error': 'escalated', 'timeout': 'escalated'}),
        'fix': State('codex', {'success': 'verify', 'error': 'escalated',
                              'timeout': 'escalated', 'budget_exceeded': 'escalated'},
                     max_state_visits_per_run=1, diagnostics_from_previous_max_bytes=4096),
        'accepted': State(terminal=True), 'escalated': State(terminal=True)})


def diagnostic_argv(executable):
    # IDs are deliberately irrelevant to prompt construction; no runtime event is created.
    request = InvocationRequest(WorkUnit(TASK), DiagnosticInput(
        DiagnosticPayload(DIAGNOSTIC_BYTES), DiagnosticOrigin('manual', 'verify', 'manual', 'manual')))
    return [executable, '--ask-for-approval', 'never', 'exec', '--ephemeral',
            '--ignore-user-config', '--ignore-rules', '--sandbox', 'workspace-write',
            '--skip-git-repo-check', '--', _prompt(request)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-real', action='store_true', required=True)
    parser.add_argument('--codex-executable', required=True)
    args = parser.parse_args(argv)
    if not Path(args.codex_executable).is_absolute():
        parser.error('--codex-executable must be absolute')
    workspace = tempfile.mkdtemp(prefix='runtime009-smoke-')
    passed = False
    try:
        print('Platform:', platform.platform())
        subprocess.run([args.codex_executable, '--version'], check=True, timeout=5)
        Path(workspace, 'result.txt').write_bytes(b'broken\n')
        verifier = CommandExecutor(Command(sys.executable, ('-c', VERIFIER_CODE), workspace, 5, 4096))
        result = run(WorkUnit(TASK), repair_flow(), {
            'verify': verifier, 'codex': CodexExecutor(CodexConfig(args.codex_executable, workspace, 120))})
        outcomes = [(e.state, e.outcome) for e in result.events if e.kind == 'invocation_finished']
        invocations = sum(e.kind == 'invocation_started' and e.state == 'fix' for e in result.events)
        for state, outcome in outcomes:
            print(f'{state}: {outcome}')
        refusals = [e for e in result.events if e.kind == 'budget_exceeded']
        for event in refusals:
            print(f'budget_exceeded: {event.state}, limit={event.limit}, observed={event.observed}')
        print('Terminal:', result.terminal_state)
        print('Runtime-scheduled Codex repair invocations:', invocations)
        passed = (outcomes == [('verify', 'failed'), ('fix', 'success'), ('verify', 'success')]
                  and result.terminal_state == 'accepted' and invocations == 1)
        if outcomes == [('verify', 'failed'), ('fix', 'success'), ('verify', 'failed')]:
            assert result.terminal_state == 'escalated' and invocations == 1
            assert len(refusals) == 1 and refusals[0].limit == refusals[0].observed == 1
    except ExecutionError as exc:
        print('ExecutionError (run failed):', type(exc.__cause__).__name__)
        for event in exc.events:
            if event.kind == 'invocation_finished': print(event.state, event.outcome)
    except Exception as exc:
        print('Smoke infrastructure failure:', type(exc).__name__)
    finally:
        print('Smoke:', 'PASS' if passed else 'FAIL')
        if passed:
            shutil.rmtree(workspace)
        else:
            print('Retained workspace / diagnostic cwd:', workspace)
            print('Diagnostic argv:', json.dumps(diagnostic_argv(args.codex_executable), ensure_ascii=False))
            print('Manual rerun only; no automatic second invocation. Rerun is outside smoke acceptance.')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
