"""Project-local delivery runner; generic Switchyard CLI and YAML stay unchanged."""
import argparse
import json
import sys
import subprocess

from runtime import ExecutionError, RunContext, WorkUnit, run
from runtime.recording import JsonlRecorder
from .acceptance import AcceptanceApproval, GitAcceptance
from .presets import build_accept, build_change


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('deliver', 'repair', 'accept', 'inspect'):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument('--workspace', required=True)
        if name in ('accept', 'inspect'):
            command.add_argument('--git-executable', required=True)
        if name == 'inspect':
            continue
        command.add_argument('--task', required=True)
        command.add_argument('--correlation-id', required=True)
        command.add_argument('--parent-run-id')
        command.add_argument('--events-file')
        if name == 'accept':
            command.add_argument('--approved-head', required=True)
            command.add_argument('--approved-patch-sha256', required=True)
            command.add_argument('--message', required=True)
            command.add_argument('--manual-qa-passed', action='store_true', required=True)
        else:
            command.add_argument('--codex-executable', required=True)
            command.add_argument('--verifier-executable', required=True)
            command.add_argument('--verifier-arg', action='append', default=[])
            command.add_argument('--agent-timeout', type=float, default=300)
            command.add_argument('--verifier-timeout', type=float, default=120)
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            snapshot = GitAcceptance(args.workspace, args.git_executable).inspect()
            inspection = {'base_commit': snapshot.head, 'patch_sha256': snapshot.patch_sha256,
                          'tree': snapshot.tree, 'branch': snapshot.branch}
        else:
            context = RunContext(args.correlation_id, args.parent_run_id)
            if context.correlation_id is None:
                raise ValueError('correlation is required')
            if args.command == 'accept':
                loaded = build_accept(workspace=args.workspace, git_executable=args.git_executable,
                                      approval=AcceptanceApproval(args.approved_head, args.approved_patch_sha256,
                                                                  args.message, args.manual_qa_passed))
            else:
                loaded = build_change(args.command, workspace=args.workspace,
                                      codex_executable=args.codex_executable,
                                      verifier_executable=args.verifier_executable,
                                      verifier_argv=tuple(args.verifier_arg),
                                      agent_timeout=args.agent_timeout, verifier_timeout=args.verifier_timeout)
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        print('delivery: invalid configuration or worktree', file=sys.stderr)
        return 2
    if args.command == 'inspect':
        print(json.dumps(inspection, sort_keys=True))
        return 0
    try:
        result = run(WorkUnit(args.task), loaded.flow, loaded.executors, context=context,
                     recorder=JsonlRecorder(args.events_file) if args.events_file is not None else None)
    except ExecutionError:
        print('delivery: runtime execution failed', file=sys.stderr)
        return 3
    start = next(e for e in result.events if e.kind == 'run_started')
    print('Run: ' + start.run_id + '\nTerminal: ' + result.terminal_state)
    return 0 if result.terminal_state in ('READY_FOR_MANUAL_QA', 'ACCEPTED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
