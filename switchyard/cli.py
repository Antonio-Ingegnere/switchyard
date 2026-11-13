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


def _count(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be a non-negative integer')
    return number


def _parser():
    parser = argparse.ArgumentParser(prog='switchyard', allow_abbrev=False)
    commands = parser.add_subparsers(dest='command', required=True)
    work = commands.add_parser('work', allow_abbrev=False)
    actions = work.add_subparsers(dest='action', required=True)
    for action, source in (('adopt', 'clean source checkout for the new work'),
                           ('transplant', 'clean source checkout whose HEAD is the new base')):
        derive = actions.add_parser(action, allow_abbrev=False)
        derive.add_argument('old_work_root'); derive.add_argument('work_root')
        derive.add_argument('--source', help=source+' (default: the old work\'s source checkout)')
        for flag in ('--config', '--claude-executable', '--codex-executable'):
            derive.add_argument(flag)
    integration = actions.add_parser('integration-status', allow_abbrev=False)
    integration.add_argument('work_root')
    integration.add_argument('--target', help='local branch, ref or commit to assess against '
                             '(default: the source branch recorded when the delivery started)')
    integration.add_argument('--json', action='store_true', help='print the structured assessment')
    integrate = actions.add_parser('integrate', allow_abbrev=False)
    integrate.add_argument('work_root')
    integrate.add_argument('--target', help='local branch to move (default: the source branch recorded when the '
                           'delivery started); must match the latest fresh integration-status assessment')
    integrate.add_argument('--json', action='store_true', help='print the structured integration record')
    push = actions.add_parser('push', allow_abbrev=False)
    push.add_argument('work_root')
    push.add_argument('--remote', required=True, help='configured Git remote to publish the local integration to')
    push.add_argument('--target', help='integrated local branch to publish to the same branch on the remote '
                      '(default: the source branch recorded when the delivery started)')
    push.add_argument('--json', action='store_true', help='print the structured push record')
    pr_create = actions.add_parser('pr-create', allow_abbrev=False)
    pr_create.add_argument('work_root')
    pr_create.add_argument('--remote', required=True, help='configured Git remote receiving the review branch; '
                           'its repository is the pull request repository')
    pr_create.add_argument('--target', help='base branch; must match the latest fresh integration-status assessment')
    pr_create.add_argument('--title', help='pull request title (default: "Switchyard delivery <work-id>")')
    pr_create.add_argument('--gh-executable', help='GitHub CLI used for the provider API (default: gh)')
    pr_create.add_argument('--json', action='store_true', help='print the structured pull request record')
    trace = actions.add_parser('trace', allow_abbrev=False)
    trace.add_argument('work_root')
    trace.add_argument('--run', help='only this run id')
    trace.add_argument('--state', help='only this (logical or runtime) state')
    trace.add_argument('--invocation', help='only this invocation id')
    trace.add_argument('--type', dest='event_type', help='only this event type')
    trace.add_argument('--json', action='store_true', help='print the trace events as a JSON array')
    diagnostics = actions.add_parser('diagnostics', allow_abbrev=False)
    diagnostics.add_argument('work_root')
    diagnostics.add_argument('--invocation', help='show one invocation in detail (full id, or I<n>)')
    diagnostics.add_argument('--json', action='store_true', help='print the diagnostic records as a JSON array')
    logs = actions.add_parser('logs', allow_abbrev=False)
    logs.add_argument('work_root')
    logs.add_argument('--run', help='only this run id')
    logs.add_argument('--state', help='only this (logical or runtime) state')
    logs.add_argument('--invocation', help='only this invocation (full id, or I<n>), followed by its diagnostic')
    logs.add_argument('--last', type=_count, help='only the last N matching events')
    logs.add_argument('--json', action='store_true', help='print the timeline entries as a JSON array')
    bundle = actions.add_parser('debug-bundle', allow_abbrev=False)
    bundle.add_argument('work_root')
    bundle.add_argument('--output', required=True, help='new ZIP file outside the work (never overwritten)')
    bundle.add_argument('--json', action='store_true', help='print the archive path, counts, hashes and completeness')
    watch = actions.add_parser('watch', allow_abbrev=False)
    watch.add_argument('work_root')
    watch.add_argument('--interval', type=float, default=1.0, help='poll interval in seconds (0.2 to 60; default 1)')
    watch.add_argument('--last', type=_count, default=10, help='recent events to show first (default 10)')
    watch.add_argument('--json', action='store_true', help='newline-delimited JSON, one self-contained object per line')
    pr_status = actions.add_parser('pr-status', allow_abbrev=False)
    pr_status.add_argument('work_root')
    pr_status.add_argument('--gh-executable', help='GitHub CLI used for the provider API (default: gh)')
    pr_status.add_argument('--json', action='store_true', help='print the structured observation')
    for action in ('status', 'answer', 'resume', 'rollback', 'qa-fix', 'accept', 'suspend'):
        operation = actions.add_parser(action, allow_abbrev=False)
        operation.add_argument('work_root')
        if action == 'answer':
            operation.add_argument('request_id')
            operation.add_argument('answer')
            operation.add_argument('--resume-to', help='select a declared human continuation target')
        if action == 'suspend':
            operation.add_argument('reason', nargs='?', default='', help='optional human reason, at most 4096 bytes')
        if action == 'qa-fix':
            operation.add_argument('feedback', nargs='?', help='one free-text manual QA finding for the current candidate')
            operation.add_argument('--finding', action='append', dest='findings', metavar='TEXT',
                                   help='one QA finding (repeatable; instead of feedback)')
            operation.add_argument('--attach', action='append', dest='attachments', metavar='PATH',
                                   help='evidence file for the single finding (repeatable)')
        if action in ('answer', 'resume', 'qa-fix'):
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


def _integration_text(a):
    short = lambda sha: sha[:12] if sha else '-'
    lines = ['Integration '+a['readiness'], 'Target: %s @ %s' % (_safe_display(a['target_ref'], 256), a['target_sha']),
             'Accepted: '+a['accepted_commit_sha'], 'Base: '+a['delivery_base_sha'], 'Drift: '+a['drift'],
             'Strategy: '+(a['strategy'] or '-').replace('_', '-')]
    if a['conflicts']:
        lines.append('Conflicts:')
        lines += ['  %s - %s' % (_safe_display(c['path'], 512), c['kind']) for c in a['conflicts']]
    else:
        lines.append('Conflicts: none')
    if a['delivery_branch_state'] != 'at_accepted':
        lines.append('Delivery branch: %s (%s @ %s)' % (a['delivery_branch_state'], a['delivery_branch'], short(a['delivery_branch_sha'])))
    lines.append('Assessment: %s (v%d)' % (a['assessment_id'], a['assessment_version']))
    return '\n'.join(lines)


def _integrated_text(r):
    present = r['integrated_by'] == 'already_present'
    return '\n'.join(['Integration '+('ALREADY_PRESENT' if present else 'INTEGRATED'),
                      'Target: %s %s -> %s' % (_safe_display(r['target_ref_full'], 256), r['target_before_sha'], r['target_after_sha']),
                      'Strategy: '+(r['strategy'] or 'none').replace('_', '-'), 'Accepted: '+r['accepted_commit_sha'],
                      'Integrated commit: '+(r['integrated_commit_sha'] or '- (already in target)'),
                      'Assessment: '+r['consumed_assessment_id']])


def _trace_detail(e):
    p = e['payload']
    if e['event_type'] == 'invocation_finished':
        return '%s  %s' % (p.get('outcome'), '%.1fs' % (p['duration_ms']/1000) if p.get('duration_ms') is not None else '-')
    if e['event_type'] == 'invocation_started':
        b = p.get('budget') or {}
        scopes = ' '.join('%s %d->%d/%d' % (s['scope'].rsplit(':', 2)[-2] if s['scope'].endswith(':state') else 'rung',
                                            s['used_before'], s['used_after'], s['limit']) for s in b.get('invocation_scopes', []))
        run = b.get('run_invocations')
        return 'launch %s attempt %s%s%s' % (p.get('invocation_in_visit'), p.get('semantic_attempt') or '-', '  run %d->%d/%d' % (run['used_before'], run['used_after'], run['limit']) if run else '',
                                   '  '+scopes if scopes else '')
    if e['event_type'] == 'transition_decided':
        return '-> %s (%s)' % (p.get('to_state'), p.get('outcome') or p.get('trigger'))
    if e['event_type'] == 'run_interrupted':
        return '%s %s' % (p.get('reason') or 'unknown', p.get('outcome_category') or '')
    if e['event_type'] == 'budget_exceeded':
        return '%s %s/%s -> %s' % (p.get('metric'), p.get('observed'), p.get('limit'), p.get('route_to'))
    if e['event_type'] == 'run_resumed':
        return '%s%s' % (p.get('resume_kind'), ' -> '+p['target'] if p.get('target') else '')
    if e['event_type'] == 'run_started':
        r, g = (p.get('provenance') or {}).get('runtime', {}), (p.get('provenance') or {}).get('git', {})
        return 'switchyard %s config %s base %s %s' % (r.get('switchyard_version'), (r.get('config_sha256') or '')[:12],
                                                      (g.get('delivery_base_sha') or '')[:12], g.get('delivery_branch') or '')
    if e['event_type'] == 'run_finished':
        return p.get('terminal') or ''
    return ''


def _cell(value, limit):
    """One printable table cell (trace text is already bounded and redacted)."""
    text = ''.join(ch if ch.isprintable() else '?' for ch in str(value))
    return text if len(text) <= limit else text[:limit-3]+'...'


def _trace_text(events):
    if not events:
        return 'no trace events available'
    runs = {}
    rows = [('SEQ', 'RUN', 'STATE', 'EVENT', 'AGENT', 'PROVIDER/MODEL', 'DETAIL')]
    for e in events:
        run = runs.setdefault(e['run_id'], 'r%d' % (len(runs)+1))
        agent = e['agent'] if e['event_type'] in ('routing_decided', 'invocation_started', 'invocation_finished', 'run_interrupted') else ''
        model = '%s/%s' % (e['provider'], e['model'] or '-') if agent else ''
        rows.append((str(e['sequence']) + ('*' if e['observed_by'] != 'runtime' else ''), run, _cell(e['state'] or '-', 32),
                     e['event_type'], agent or '', model, _cell(_trace_detail(e), 160)))
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    lines = ['  '.join(r[i].ljust(widths[i]) for i in range(6)) + '  ' + r[6] for r in rows]
    if any(e['observed_by'] != 'runtime' for e in events):
        lines.append('* reconstructed by recovery backfill from durable records')
    return '\n'.join(line.rstrip() for line in lines)


def _short(invocation_id):
    return 'I'+invocation_id.rsplit(':', 1)[-1]


def _result_label(r):
    result = r.get('result')
    if result is None:
        return 'unknown'
    if result.get('produced_stage_result') and not result.get('reason'):
        return result.get('outcome') or 'unknown'
    return result.get('classification') or result.get('reason') or result.get('outcome_category') or 'unknown'


def _seconds(ms):
    return '-' if ms is None else '%dms' % ms if ms < 1000 else '%.1fs' % (ms/1000)


def _diagnostics_text(records):
    """Failed invocations first (each group in invocation order); details via --invocation."""
    from .diagnostics import failed
    if not records:
        return 'no diagnostics available'
    runs = {r['run_id'] for r in records}
    rows = [('INVOCATION', 'STATE', 'AGENT', 'PROVIDER', 'RESULT', 'DURATION', 'DIAGNOSTIC')]
    bad = [r for r in records if failed(r)]
    for r in bad + [r for r in records if not failed(r)]:
        label = _short(r['invocation_id']) if len(runs) == 1 else r['invocation_id']
        rows.append((label, _cell(r.get('state') or '-', 32), _cell(r.get('agent') or '-', 32), _cell(r.get('provider') or '-', 16),
                     _cell(_result_label(r), 40), _seconds(r.get('duration_ms')), r.get('completeness')))
    widths = [max(len(row[i]) for row in rows) for i in range(6)]
    lines = ['%d invocation(s), %d failed' % (len(records), len(bad))]
    lines += ['  '.join(row[i].ljust(widths[i]) for i in range(6)) + '  ' + row[6] for row in rows]
    return '\n'.join(line.rstrip() for line in lines)


def _diagnostic_detail(r):
    result = r.get('result') or {}
    p = r.get('process') or {}
    lines = ['Work: %s' % (r.get('work_id') or '-'), 'Run: %s' % r['run_id'],
             'Invocation: %s (%s)' % (r['invocation_id'], _short(r['invocation_id'])),
             'State: %s (rung %s, visit %s)' % (r.get('state'), r.get('rung'), r.get('state_visit_id')),
             'Agent: %s' % (r.get('agent') or '-'), 'Provider: %s' % (r.get('provider') or '-'), 'Model: %s' % (r.get('model') or '-'), '']
    if r.get('completeness') == 'unavailable':
        return '\n'.join(lines + ['Diagnostic: unavailable', '  '+_cell(r.get('summary') or '', 512)])
    produced = result.get('produced_stage_result')
    kind = ('semantic outcome (stage result produced)' if produced and not result.get('reason') else
            'stage result produced, then the runtime stopped' if produced else
            'no stage result' if produced is False else 'unknown')
    lines += ['Outcome: %s (%s)' % (_result_label(r), kind)]
    if result.get('classification') and result.get('reason'):
        lines.append('Failure: %s, classification %s' % (result['reason'], result['classification']))
    charged = result.get('semantic_invocation')
    lines.append('Charged as attempt: %s' % ('yes' if charged else 'no (infrastructure failure: not a semantic attempt)'
                                             if charged is False else 'unknown'))
    lines.append('Exit code: %s' % ('-' if p.get('exit_code') is None else p['exit_code']))
    if p.get('termination_signal'):
        lines.append('Termination: %s (%s)' % (p['termination_signal'], (p.get('terminated_by') or 'unknown').replace('_', ' ')))
    if p.get('launched') is False:
        lines.append('Launch: the process could not be started')
    lines += ['Duration: %s%s' % (_seconds(r.get('duration_ms')), ' (%s)' % r['duration_source'].replace('_', ' ') if r.get('duration_source') else ''),
              'Timeout configured: %s' % ('-' if r.get('timeout_seconds') is None else '%gs' % r['timeout_seconds']), '',
              'Diagnostic:', '  '+_cell(r.get('summary') or '-', 2048)]
    if result.get('error'):
        lines.append('  Error: '+_cell(result['error'], 2048))
    channel = r.get('provider_channel')
    if channel:
        facts = ', '.join('%s=%s' % (k, channel[k]) for k in ('subtype', 'terminal_reason', 'is_error', 'api_error_status', 'api_error_code', 'num_turns') if k in channel)
        lines += ['', 'Provider result channel:', '  '+(facts or 'no documented result envelope')]
        lines += ['  error: '+_cell(m, 512) for m in channel.get('errors', ())]
        if channel.get('errors_omitted'):
            lines.append('  %d error message(s) omitted (%s)' % (channel['errors_omitted']['count'], channel['errors_omitted']['reason']))
        if channel.get('permission_denials'):
            lines.append('  permission denials: '+', '.join(channel['permission_denials']))
    s = r.get('structured_output')
    lines += ['', 'Structured output:']
    if s is None:
        lines.append('  not applicable' if r.get('provider') == 'command' else '  unknown')
    else:
        repair = s.get('repair') or {}
        lines.append('  Schema: %s (%s) sha256 %s' % (s['schema']['name'], s['schema']['transport'], s['schema']['sha256'][:12]))
        lines.append('  Valid: %s' % {True: 'yes', False: 'no', None: 'not evaluated (no result was returned)'}[s.get('valid')])
        for e in s.get('errors') or ():
            lines.append('  Validation failure: %s: %s' % (e.get('path'), e.get('error')))
        if s.get('errors_truncated'):
            lines.append('  (further validation errors truncated)')
        lines.append('  Repair: %s' % ('none (single structured result)' if repair.get('mechanism') == 'none' else
                                       '%s, up to %s attempts in this invocation; attempted %s, succeeded %s' % (
                                           repair.get('mechanism'), repair.get('max_attempts'),
                                           {True: 'yes', False: 'no', None: 'unknown'}[repair.get('attempted')],
                                           {True: 'yes', False: 'no', None: 'unknown'}[repair.get('succeeded')])))
    x = r.get('stderr_excerpt') or {}
    lines += ['', 'stderr: not retained (%s%s)' % (x.get('reason'), ', %d bytes seen' % x['bytes_seen'] if x.get('bytes_seen') is not None else '')]
    v = r.get('verification')
    lines += ['', 'Verification:']
    if v is not None:
        lines.append('  Verifier %s (%s, argv sha256 %s): exit %s%s -> %s (%s)' % (
            v['verifier'], v['executable'], v['argv_sha256'][:12], v['exit_code'], ', timed out' if v.get('timed_out') else '',
            v.get('outcome'), (v.get('outcome_rule') or 'unknown').replace('_', ' ')))
        for ref in v.get('evidence_refs') or ():
            lines.append('  Evidence: %s v%s %s sha256 %s (%d bytes)' % (ref['name'], ref['version'], ref['path'], ref['sha256'][:12], ref['byte_size']))
    elif r.get('followed_by_verification'):
        f = r['followed_by_verification']
        lines.append('  Next: %s %s -> %s (exit %s)' % (_short(f['invocation_id']), f['verifier'], f['outcome'], f['exit_code']))
    else:
        lines.append('  Not executed')
    lines += ['', 'Completeness: %s (%s)%s' % (r['completeness'], r['observed_by'].replace('_', ' '), ', truncated' if r.get('truncated') else '')]
    return '\n'.join(lines)


def _pr_text(o, created_by=None):
    lines = ['Pull request %s #%d%s%s' % (o['state'], o['number'], ' (draft)' if o['draft'] else '',
                                         ' (%s)' % created_by if created_by else ''),
             'URL: '+_safe_display(o['url'], 512),
             'Head: %s expected %s, remote %s%s' % (o['head_ref'], o['head_expected_sha'], o['remote_head_sha'] or 'missing',
                                                   ' (DRIFT)' if o['head_drift'] else ''),
             'Base: %s at %s' % (o['base_ref'], o['base_sha'] or '-'),
             'Mergeability: '+o['mergeability']]
    if o['merged_commit_sha']:
        lines.append('Merged commit: '+o['merged_commit_sha'])
    return '\n'.join(lines)


def _work_command(args):
    from .recovery import (recovery_status, answer_v2_work, resume_v2_work, rollback_v2_work,
                           qa_fix_v2_work, accept_v2_candidate)
    from .provider_stages import ProviderRuntimeConfig
    try:
        if args.action in ('adopt', 'transplant'):
            from .continuity import adopt_v2_work, transplant_v2_work, default_source, B, TRANSPLANT_BLOCKED
            # Default: the old work's recorded source checkout, read from its frozen baseline only (never its
            # live status, which may reconcile and write into the old work).
            source = args.source or default_source(args.old_work_root, TRANSPLANT_BLOCKED if args.action == 'transplant' else B)
            config_text = (Path(args.config) if args.config else Path(source)/'.switchyard/config.yaml').read_text(encoding='utf-8')
            derive = transplant_v2_work if args.action == 'transplant' else adopt_v2_work
            derive(args.old_work_root, args.work_root, config_text,
                   ProviderRuntimeConfig(source, args.claude_executable, args.codex_executable))
            print(json.dumps(recovery_status(args.work_root), ensure_ascii=True, indent=2))
            return 0
        if args.action == 'integration-status':
            from .integration import integration_readiness, INTEGRABLE
            assessment = integration_readiness(args.work_root, args.target)
            print(json.dumps(assessment, ensure_ascii=True, indent=2) if args.json else _integration_text(assessment))
            return 0 if assessment['readiness'] in INTEGRABLE else 1
        if args.action == 'push':
            from .publication import push_v2_work
            record = push_v2_work(args.work_root, args.remote, args.target)
            print(json.dumps(record, ensure_ascii=True, indent=2) if args.json else '\n'.join([
                'Push '+('ALREADY_PRESENT' if record['pushed_by'] == 'already_present' else 'PUSHED'),
                'Remote: %s (%s)' % (record['remote'], _safe_display(record['remote_identity'], 512)),
                'Ref: %s %s -> %s' % (record['remote_ref'], record['remote_before_sha'], record['remote_after_sha']),
                'Integrated commit: '+record['local_integrated_commit_sha']]))
            return 0
        if args.action == 'trace':
            from .trace import trace_events
            events = trace_events(args.work_root, args.run, args.state, args.invocation, args.event_type)
            print(json.dumps(events, ensure_ascii=True, indent=2, sort_keys=True) if args.json else _trace_text(events))
            return 0
        if args.action == 'logs':
            from . import logs
            events, held = logs.log_events(args.work_root, args.run, args.state, args.invocation, args.last)
            if args.json:
                print(json.dumps([logs.entry(Path(args.work_root), e) for e in events], ensure_ascii=True, indent=2, sort_keys=True))
                if held is not None:
                    print('switchyard: warning: '+logs.gap_warning(held), file=sys.stderr)
                return 0
            print(logs.render_logs(Path(args.work_root), events, held))
            if args.invocation is not None:
                from .diagnostics import read_diagnostics
                records = [r for r in read_diagnostics(args.work_root, args.invocation) if r.get('recorded')]
                print('\n' + ('\n\n'.join(_diagnostic_detail(r) for r in records) if records else 'Diagnostic unavailable'))
            return 0
        if args.action == 'debug-bundle':
            from .bundle import create_bundle
            summary = create_bundle(args.work_root, args.output)
            if args.json:
                print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
            else:
                c, k = summary['counts'], summary['completeness']
                print('\n'.join(['Debug bundle created: %s' % _safe_display(summary['path'], 1024), '',
                                 'Trace events:        %d' % c['events'], 'Diagnostics:         %d' % c['diagnostics'],
                                 'Trace completeness:  %s' % k['trace'], 'Diagnostics:         %s' % k['diagnostics'],
                                 'History:             %s' % k['history'], 'Issues:              %d' % sum(summary['issue_counts'].values()),
                                 '', 'Source code:         excluded', 'Provider payloads:   excluded',
                                 'Secrets:             not intentionally included', 'SHA-256:             %s' % summary['sha256']]))
            return 0
        if args.action == 'watch':
            from . import logs
            try:
                return logs.watch(args.work_root, sys.stdout, args.interval, args.last, args.json)
            except KeyboardInterrupt:
                print('switchyard: watch stopped; the work was not affected', file=sys.stderr, flush=True)
                return 130
        if args.action == 'diagnostics':
            from .diagnostics import read_diagnostics
            records = read_diagnostics(args.work_root, args.invocation)
            if args.json:
                print(json.dumps(records, ensure_ascii=True, indent=2, sort_keys=True))
            elif args.invocation is not None:
                print('\n\n'.join(_diagnostic_detail(r) for r in records) or 'no such invocation')
            else:
                print(_diagnostics_text(records))
            return 0 if records or args.invocation is None else 1
        if args.action in ('pr-create', 'pr-status'):
            from . import pull_request
            provider = pull_request.default_provider(args.gh_executable)
            if args.action == 'pr-create':
                record = pull_request.pr_create_v2_work(args.work_root, args.remote, args.target, args.title, provider)
                print(json.dumps(record, ensure_ascii=True, indent=2) if args.json else _pr_text(dict(
                    record['observed'], url=record['pull_request']['url'], target_ref=record['target_ref']), record['created_by']))
            else:
                observed = pull_request.pr_status_v2_work(args.work_root, provider)
                print(json.dumps(observed, ensure_ascii=True, indent=2) if args.json else _pr_text(observed))
            return 0
        if args.action == 'integrate':
            from .integration import integrate_v2_work
            record = integrate_v2_work(args.work_root, args.target)
            print(json.dumps(record, ensure_ascii=True, indent=2) if args.json else _integrated_text(record))
            return 0
        status = recovery_status(args.work_root)
        if args.action == 'rollback':
            status = rollback_v2_work(args.work_root)
        elif args.action == 'suspend':
            from .suspend import suspend_v2_work
            status = suspend_v2_work(args.work_root, args.reason)
        elif args.action == 'accept':
            accept_v2_candidate(args.work_root)
            status = recovery_status(args.work_root)
        elif args.action in ('answer', 'resume', 'qa-fix'):
            # Isolated work: the user edits config in their source checkout; the runtime itself routes
            # execution into the recorded delivery worktree.
            checkout = status.get('source_worktree') or status['target']
            config_path = Path(args.config) if args.config else Path(checkout)/'.switchyard/config.yaml'
            config_text = config_path.read_text(encoding='utf-8')
            provider = ProviderRuntimeConfig(checkout, args.claude_executable, args.codex_executable)
            if args.action == 'qa-fix':
                qa_fix_v2_work(args.work_root, args.feedback, config_text, provider, findings=args.findings,
                               attachments=args.attachments)
            elif args.action == 'answer':
                answer_v2_work(args.work_root, args.request_id, args.answer, config_text, provider, resume_to=args.resume_to)
            else:
                resume_v2_work(args.work_root, config_text, provider)
            status = recovery_status(args.work_root)
    except (ValueError, OSError, ExecutionError) as exc:
        print('switchyard: '+_safe_display(str(exc), 2048), file=sys.stderr)
        if getattr(exc, 'transplant', None):
            print(json.dumps(exc.transplant, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        if getattr(exc, 'pull_request', None):
            print(json.dumps(exc.pull_request, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        if getattr(exc, 'push', None):
            print(json.dumps(exc.push, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        if getattr(exc, 'integration', None):
            print(json.dumps(exc.integration, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        if getattr(exc, 'continuity_plan', None):
            print(json.dumps(exc.continuity_plan, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        if getattr(exc, 'human_input_request', None):
            print(json.dumps(exc.human_input_request, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(status, ensure_ascii=True, indent=2))
    return 0
