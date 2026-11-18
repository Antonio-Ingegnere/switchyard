"""ECONOMICS (#2) slice 2 (ECONOMICS-002): composition of the prompt Switchyard itself supplies, one record per invocation.

Observational, never authority: the adapter measures the bytes of the one render it transmits on stdin
(provider_stages.render_prompt), the diagnostics writer publishes the measurement after the authoritative records
(failure-isolated), and `work prompt-estimate` only reads the immutable records.

Scope: `switchyard_supplied_initial_prompt`. Exact UTF-8 bytes and characters per code-defined category, plus a
heuristic token estimate (`heuristic_chars_per_token_v1`) that is not provider-tokenizer accurate and is never usage,
cost or billing. Provider-internal instructions, tool-acquired repository context, tool output, subagent context,
repair turns and provider caching are unobserved. The native output schema and image attachments travel outside stdin:
their byte sizes are transport metadata, never added to the prompt and never converted to tokens.

Storage: `<work>/observability/prompt-estimates/<invocation id, ':' -> '_'>.json`, immutable, published with an
exclusive link. Records hold numbers, code-defined labels and runtime identifiers only: no prompt text, artifact
names, paths, request, MicroSpec, handoff, QA, source or evidence content.
"""
import hashlib
import json
import os
from pathlib import Path

from .provider_stages import CHARS_PER_TOKEN, PROMPT_CATEGORIES, PROMPT_ESTIMATOR
from .trace import _publish

SCHEMA_VERSION = 1
REPORT_SCHEMA = 1
MAX_RECORD = 16384
SCOPE = 'switchyard_supplied_initial_prompt'
STATUSES = ('submitted_or_launched', 'prepared_but_not_launched', 'capture_unavailable', 'not_applicable')
FIRST_RECORDING_VERSION = (2, 10, 0)
LEGACY = 'historical_prompt_composition_not_captured'
_UNOBSERVED = ('provider_internal_context', 'provider_system_instructions', 'tool_generated_context',
               'repository_tool_context', 'provider_generated_messages', 'provider_prompt_caching')
UNOBSERVED = dict(claude=_UNOBSERVED+('structured_output_repair_turns', 'subagent_context'),
                  codex=_UNOBSERVED+('shell_command_output', 'additional_agent_threads'))


def file_name(invocation_id):
    return invocation_id.replace(':', '_')+'.json'


def _directory(root):
    return Path(root)/'observability'/'prompt-estimates'


def estimate_id(work_id, invocation_id):
    return hashlib.sha256('\0'.join((work_id, invocation_id, 'prompt-estimate')).encode('utf-8')).hexdigest()[:32]


def _status(where, facts):
    prompt = getattr(facts, 'prompt', None) if facts is not None else None
    if where.get('provider') == 'command':
        return 'not_applicable', 'verifier_command', None
    if facts is None:
        return 'capture_unavailable', 'not_captured', None  # Recovery: the measurement died with the controller.
    if type(prompt) is not dict:
        return 'capture_unavailable', 'prompt_not_prepared', None  # Failed before rendering: no zero-length prompt.
    if (facts.process or {}).get('launched') is True:
        return 'submitted_or_launched', None, prompt  # Launched with the bytes on stdin; consumption is not proven.
    return 'prepared_but_not_launched', None, prompt


def build(work_id, event, where, facts, observed_by):
    """The composition record of one ended invocation (`where`: its state, agent, provider, ... location)."""
    status, reason, prompt = _status(where, facts)
    exact = components = estimate = transport = None
    if prompt is not None:
        exact = dict(utf8_bytes=prompt['utf8_bytes'], characters=prompt['characters'], sections=prompt['sections'])
        components = [dict(category=c['category'], utf8_bytes=c['utf8_bytes'], characters=c['characters'],
                           estimated_tokens=c['estimated_tokens'], items=c['items'])
                      for c in prompt['components'] if c['category'] in PROMPT_CATEGORIES]
        if prompt['measurement'] != 'exact' or sum(c['utf8_bytes'] for c in components) != exact['utf8_bytes']:
            components, reason = [], 'composition_measurement_failed'  # Totals stay exact; no guessed breakdown.
        estimate = dict(prompt['estimate'], provider_tokenizer=False, basis='characters') if components else None
        transport = {k: prompt['transport'][k] for k in ('prompt_channel', 'native_output_schema_bytes', 'native_output_schema_channel',
                                                       'image_attachment_count', 'image_attachment_total_bytes')}
    provider = where.get('provider')
    return dict(
        schema_version=SCHEMA_VERSION, record='prompt_composition', estimate_id=estimate_id(work_id, event.invocation_id),
        work_id=work_id, run_id=event.run_id, state_visit_id=event.state_visit_id, invocation_id=event.invocation_id,
        state=where.get('state'), runtime_state=where.get('runtime_state'), rung=where.get('rung'), agent=where.get('agent'),
        provider=provider, configured_model=where.get('model'), resolved_model=(prompt or {}).get('resolved_model'),
        capture_status=status, reason=reason, scope=SCOPE if provider != 'command' else None,
        exact=exact, components=components or [], estimate=estimate, transport=transport,
        unobserved=list(UNOBSERVED.get(provider, ())) if provider != 'command' else [], observed_by=observed_by)


def write(root, record):
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory/'schema.json').exists():
        _publish(directory/'schema.json', dict(schema_version=SCHEMA_VERSION, record='prompt_composition', scope=SCOPE,
                                               categories=list(PROMPT_CATEGORIES), capture_statuses=list(STATUSES),
                                               estimator=dict(method=PROMPT_ESTIMATOR, characters_per_token=CHARS_PER_TOKEN)))
    path = directory/file_name(record['invocation_id'])
    if path.exists():
        return  # Finalized: never overwritten by a retry, a resume or a repeated recovery.
    if len(json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(',', ':'))) > MAX_RECORD:
        raise ValueError('prompt composition record exceeds its bound')
    _publish(path, record)


# --- Read API (read-only: no rendering, no artifact reads, no backfill, no recovery, no provider, no Git) ----------
def _recording_runtime(root):
    """Whether the work was created by a runtime that records prompt composition (frozen baseline; unknown = no)."""
    from .logs import latest_record
    try:
        _, baseline = latest_record(Path(root)/'recovery', 'baseline')
        return tuple(int(p) for p in str(baseline.get('runtime_version')).split('.')[:3]) >= FIRST_RECORDING_VERSION
    except (AttributeError, TypeError, ValueError, OSError):
        return False


def _valid(record, name):
    if type(record) is not dict or record.get('schema_version') != SCHEMA_VERSION or record.get('record') != 'prompt_composition':
        return False
    if name != file_name(record.get('invocation_id') or '') or record.get('capture_status') not in STATUSES:
        return False
    exact, components = record.get('exact'), record.get('components')
    if exact is None:
        return components == []
    return (type(components) is list and all(type(c) is dict and c.get('category') in PROMPT_CATEGORIES for c in components)
            and (not components or sum(c.get('utf8_bytes') or 0 for c in components) == exact.get('utf8_bytes')))


def read_estimates(work_root, run=None, state=None, agent=None, provider=None, model=None, invocation=None):
    """(records, recorded): one composition per invocation in canonical order. An invocation without a valid record is
    listed (never persisted) as `capture_unavailable` with reason `not_recorded`, or `historical_prompt_composition_not_
    captured` when the work has no store and was created before this runtime recorded composition."""
    from .diagnostics import _invocations, _match
    root = Path(work_root)
    directory = _directory(root)
    recorded = directory.is_dir() or _recording_runtime(root)
    stored = {}
    if directory.is_dir():
        for name in sorted(os.listdir(directory)):
            if not name.endswith('.json') or name == 'schema.json':
                continue
            try:
                record = json.loads((directory/name).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if _valid(record, name):
                stored[record['invocation_id']] = dict(record, recorded=True)
    order, located, _ = _invocations(root)
    out = []
    for event in order:
        record = stored.pop(event.invocation_id, None)
        if record is None:
            where = located.get(event.invocation_id) or {}
            command = where.get('provider') == 'command'
            record = dict(schema_version=SCHEMA_VERSION, record='prompt_composition', estimate_id=None,
                          work_id=where.get('work_id'), run_id=event.run_id, state_visit_id=event.state_visit_id,
                          invocation_id=event.invocation_id, state=where.get('state', event.state), runtime_state=event.state,
                          rung=where.get('rung'), agent=where.get('agent'), provider=where.get('provider'),
                          configured_model=where.get('model'), resolved_model=None,
                          capture_status='not_applicable' if command else 'capture_unavailable',
                          reason='verifier_command' if command else ('not_recorded' if recorded else LEGACY),
                          scope=None if command else SCOPE, exact=None, components=[], estimate=None, transport=None,
                          unobserved=[] if command else list(UNOBSERVED.get(where.get('provider'), _UNOBSERVED)),
                          observed_by=None, recorded=False)
        out.append(record)
    out.extend(stored.values())
    return [r for r in out
            if (run is None or r['run_id'] == run) and (state is None or state in (r['state'], r['runtime_state']))
            and (agent is None or r['agent'] == agent) and (provider is None or r['provider'] == provider)
            and (model is None or model in (r['configured_model'], r['resolved_model']))
            and _match(r['invocation_id'], invocation)], recorded


def summarize(records):
    """Totals over the selected invocations: REPEATED SUBMISSIONS, not unique information. A MicroSpec sent in five
    invocations counts five times. Unknown is never zero: an unmeasured invocation adds nothing and is counted apart."""
    measured = [r for r in records if r['exact'] is not None]
    broken = [r for r in measured if not r['components']]
    categories = []
    for category in PROMPT_CATEGORIES:
        rows = [c for r in measured for c in r['components'] if c['category'] == category]
        if rows:
            categories.append(dict(category=category, utf8_bytes=sum(c['utf8_bytes'] for c in rows),
                                   characters=sum(c['characters'] for c in rows),
                                   estimated_tokens=sum(c['estimated_tokens'] for c in rows), invocations=len(rows)))
    ranked = sorted(categories, key=lambda c: (-c['utf8_bytes'], PROMPT_CATEGORIES.index(c['category'])))
    composed = [r for r in measured if r['components']]
    statuses = {s: sum(r['capture_status'] == s for r in records) for s in STATUSES}
    transport = [r['transport'] for r in measured if r['transport']]
    return dict(
        invocations=len(records), provider_invocations=len(records)-statuses['not_applicable'], capture_status=statuses,
        measured=len(measured), composition_unavailable=len(broken),
        utf8_bytes=sum(r['exact']['utf8_bytes'] for r in measured) if measured else None,
        characters=sum(r['exact']['characters'] for r in measured) if measured else None,
        estimated_tokens=sum(r['estimate']['tokens'] for r in composed) if composed else None,
        estimated_tokens_invocations=len(composed), by_category=categories,
        largest_components=[c['category'] for c in ranked], aggregation='repeated_submissions',
        native_output_schema_bytes=sum(t['native_output_schema_bytes'] for t in transport) if transport else None,
        image_attachments=dict(count=sum(t['image_attachment_count'] for t in transport) if transport else None,
                               total_bytes=sum(t['image_attachment_total_bytes'] for t in transport) if transport else None))


def _series(records):
    """Change in transmitted bytes from the previous measured invocation of the same logical state (exact numbers)."""
    previous, out = {}, {}
    for r in records:
        if r['exact'] is None:
            continue
        before = previous.get(r['state'])
        out[r['invocation_id']] = None if before is None else dict(
            previous_invocation_id=before['invocation_id'], utf8_bytes_delta=r['exact']['utf8_bytes']-before['exact']['utf8_bytes'])
        previous[r['state']] = r
    return out


def report(work_root, **filters):
    """The versioned `work prompt-estimate` report (schema 1). Read-only; no prompt is rendered again."""
    root = Path(work_root)
    if not (root/'recovery').is_dir() and not (root/'events.jsonl').is_file():
        raise ValueError('not a Switchyard work: %s' % root.name)
    records, recorded = read_estimates(root, **filters)
    series = _series(records)
    flags = []
    if any(r['reason'] == LEGACY for r in records):
        flags.append(LEGACY)
    if any(r['capture_status'] == 'capture_unavailable' for r in records):
        flags.append('prompt_composition_partial')
    if any(r['provider'] != 'command' for r in records):
        flags += ['repository_tool_context_unobserved', 'provider_internal_context_unobserved']
    return dict(
        schema_version=REPORT_SCHEMA, report='switchyard_prompt_composition',
        work_id=next((r['work_id'] for r in records if r['work_id']), None), composition_recorded=recorded, scope=SCOPE,
        estimation=dict(method=PROMPT_ESTIMATOR, characters_per_token=CHARS_PER_TOKEN, provider_tokenizer=False,
                        accuracy='heuristic_not_provider_tokenizer_accurate', used_for_cost=False),
        invocations=[dict(r, change_from_previous_same_state=series.get(r['invocation_id'])) for r in records],
        summary=summarize(records), coverage_flags=flags,
        actual_usage='separately_reported_by_work_usage', estimated_cost=None,
        repository_tool_context='unobserved')
