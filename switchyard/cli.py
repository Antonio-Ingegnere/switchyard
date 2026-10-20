"""Thin CLI: configuration and execution semantics belong to existing libraries."""

import argparse
import json
import os
import sys
from pathlib import Path

from flow_config import FlowConfigError, load_flow
from runtime import ExecutionError, RunContext, WorkUnit, run
from runtime.recording import JsonlRecorder


def _safe_display(value: str, limit: int | None = None) -> str:
    if limit is not None:
        value = value[:limit]
    ambiguous = (not value or any(not char.isprintable() or char.isspace()
                                 or char in ',"\\' for char in value))
    rendered = json.dumps(value, ensure_ascii=True) if ambiguous else value
    if limit is not None and len(rendered) > limit:
        rendered = rendered[:limit-3] + '...'
    return rendered


def _parser():
    parser = argparse.ArgumentParser(prog='switchyard', allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    work = commands.add_parser('work', allow_abbrev=False)
    actions = work.add_subparsers(dest='action', required=True)
    for action in ('status', 'answer', 'resume', 'rollback'):
        operation = actions.add_parser(action, allow_abbrev=False)
        operation.add_argument('work_root')
        if action == 'answer':
            operation.add_argument('request_id')
            operation.add_argument('answer')
        if action in ('answer', 'resume'):
            operation.add_argument('--config')
            operation.add_argument('--claude-executable')
            operation.add_argument('--codex-executable')
    config = commands.add_parser('config', allow_abbrev=False)
    config.add_argument('action', choices=('validate', 'show'))
    config.add_argument('config_file', nargs='?', default='.switchyard/config.yaml')
    validate = commands.add_parser('validate', allow_abbrev=False)
    validate.add_argument('flow_file')
    execute = commands.add_parser('run', allow_abbrev=False)
    execute.add_argument('flow_file')
    execute.add_argument('--task', required=True)
    execute.add_argument('--require-terminal', action='append', default=[], metavar='NAME')
    execute.add_argument('--events-file', metavar='PATH',
                         help='append runtime events as JSONL (parent directory must exist)')
    execute.add_argument('--correlation-id', metavar='VALUE', help='opaque external lifecycle identifier')
    execute.add_argument('--parent-run-id', metavar='VALUE', help='opaque parent run identifier')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'work':
        return _work_command(args)
    if args.command == 'config':
        from .simple_config import load_simple_config
        try:
            config = load_simple_config(args.config_file)
        except FlowConfigError as exc:
            print('switchyard: '+_safe_display(str(exc), 512), file=sys.stderr)
            return 2
        print(json.dumps(config.show(), indent=2) if args.action == 'show' else 'Valid project configuration')
        return 0
    try:
        loaded = load_flow(args.flow_file)
    except FlowConfigError as exc:
        message = _safe_display(str(exc), 500)
    else:
        message = None
    # Output errors must not be mistaken for errors raised by load_flow/run.
    if message is not None:
        print('switchyard: ' + message, file=sys.stderr, flush=True)
        return 2
    if args.command == 'validate':
        print('Valid: ' + _safe_display(os.path.abspath(args.flow_file)), flush=True)
        return 0
    required = list(dict.fromkeys(args.require_terminal))
    for name in required:
        if name not in loaded.flow.states:
            prefix = 'switchyard: required terminal does not exist: '
        elif not loaded.flow.states[name].terminal:
            prefix = 'switchyard: required state is not terminal: '
        else:
            continue
        print(prefix + _safe_display(name, 512-len(prefix)), file=sys.stderr, flush=True)
        return 2
    options = {}
    if args.correlation_id is not None or args.parent_run_id is not None:
        try:
            context = RunContext(args.correlation_id, args.parent_run_id)
        except (TypeError, ValueError):
            invalid_context = True
        else:
            invalid_context = False
        if invalid_context:
            print('switchyard: invalid run context', file=sys.stderr, flush=True)
            return 2
        options['context'] = context
    work = WorkUnit(args.task)
    if args.events_file is not None:
        options['recorder'] = JsonlRecorder(args.events_file)
    try:
        result = run(work, loaded.flow, loaded.executors, **options)
    except ExecutionError:
        failed = True
    else:
        failed = False
    if failed:
        print('switchyard: runtime execution failed', file=sys.stderr, flush=True)
        return 3
    starts = [event for event in result.events if event.kind == 'run_started']
    if len(starts) != 1:
        raise RuntimeError('expected exactly one run_started event')
    matches = not required or result.terminal_state in required
    lines = ['Run: ' + starts[0].run_id, 'Terminal: ' + _safe_display(result.terminal_state)]
    if not matches:
        lines.append('Required terminal: ' + ', '.join(_safe_display(name) for name in required))
    print('\n'.join(lines), flush=True)
    return 0 if matches else 1


def _work_command(args):
    from .recovery import recovery_status, answer_v2_work, resume_v2_work, rollback_v2_work
    from .provider_stages import ProviderRuntimeConfig
    try:
        status = recovery_status(args.work_root)
        if args.action == 'rollback':
            status = rollback_v2_work(args.work_root)
        elif args.action in ('answer', 'resume'):
            config_path = Path(args.config) if args.config else Path(status['target'])/'.switchyard/config.yaml'
            config_text = config_path.read_text(encoding='utf-8')
            provider = ProviderRuntimeConfig(status['target'], args.claude_executable, args.codex_executable)
            if args.action == 'answer':
                answer_v2_work(args.work_root, args.request_id, args.answer, config_text, provider)
            else:
                resume_v2_work(args.work_root, config_text, provider)
            status = recovery_status(args.work_root)
    except (ValueError, OSError, ExecutionError) as exc:
        print('switchyard: '+_safe_display(str(exc), 2048), file=sys.stderr)
        if getattr(exc, 'human_input_request', None):
            print(json.dumps(exc.human_input_request, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(status, ensure_ascii=True, indent=2))
    return 0
