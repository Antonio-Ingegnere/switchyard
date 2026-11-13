"""ECONOMICS (#2) slice 1: provider-reported token usage, one durable record per invocation.

Observational accounting, never authority and never part of a delivery's success condition: written by the
diagnostics writer's lifecycle (after the authoritative records, failure-isolated), read by `work usage`.

Sources: `provider_reported` (the adapter's normalized provider usage, see provider_stages.claude_usage /
codex_usage), `unavailable` (no trustworthy usage: not reported, not launched, killed, invalid, or not captured), and
`not_applicable` (command verifiers consume no model tokens). `estimated` is reserved for ECONOMICS-002 and never
written. Counts are nullable integers: null means unknown and is never turned into zero. Provider token categories
are not additive across providers (Claude input excludes cache tokens; Codex input includes cache reads), so totals
are kept per category and the derived "input including cache" is computed per record from its own semantics.

Storage: `<work>/observability/usage/<invocation id, ':' -> '_'>.json`, immutable, published with an exclusive link.
"""
import hashlib
import json
import os
from pathlib import Path

from .trace import _publish  # Bound at import: the usage store's publication fails independently of the trace's.

SCHEMA_VERSION = 1
SOURCES = ('provider_reported', 'estimated', 'unavailable', 'not_applicable')  # 'estimated': reserved (ECONOMICS-002).
CATEGORIES = ('input', 'output', 'cache_read', 'cache_write')
MAX_RECORD = 16384
DIMENSIONS = ('run', 'state', 'agent', 'provider', 'model', 'invocation')


def file_name(invocation_id):
    return invocation_id.replace(':', '_')+'.json'


def _directory(root):
    return Path(root)/'observability'/'usage'


def usage_id(work_id, invocation_id):
    return hashlib.sha256('\0'.join((work_id, invocation_id, 'usage')).encode('utf-8')).hexdigest()[:32]


def _input_including_cache(tokens, semantics):
    """Derived per record from its provider's documented semantics; None when not provable."""
    if tokens['input'] is None or not semantics:
        return None
    if semantics.get('input_includes_cache') is True:
        # Codex: cached reads are inside input; a reported nonzero cache write has an unverified relation to input.
        return tokens['input'] if semantics.get('cache_write_in_input') is True or not tokens['cache_write'] else None
    if semantics.get('input_includes_cache') is False and tokens['cache_read'] is not None and tokens['cache_write'] is not None:
        return tokens['input'] + tokens['cache_read'] + tokens['cache_write']
    return None


def _output_detail(reported, source):
    """Provider-reported SUBSETS of output (Codex reasoning output); never an additional quantity. Additive, optional
    field (2.6.1): records written by 2.6.0 lack it and read as unknown."""
    detail = reported.get('output_detail') if source == 'provider_reported' else None
    if type(detail) is not dict:
        return None
    value = detail.get('reasoning_output')
    return dict(reasoning_output=value if type(value) is int and value >= 0 else None)


def build(work_id, event, where, facts, observed_by, configured_model):
    """The usage record for one ended invocation (`where`: its state, agent, provider, ... location)."""
    process = (facts.process if facts is not None else None) or {}
    reported = getattr(facts, 'usage', None) if facts is not None else None
    if where.get('provider') == 'command':
        reported = dict(source='not_applicable', reason='verifier_command')
    elif facts is None:
        reported = dict(source='unavailable', reason='not_captured')  # Recovery: no durable usage evidence exists.
    elif process.get('launched') is False:
        reported = dict(source='unavailable', reason='not_launched')
    elif reported is None:
        reported = dict(source='unavailable', reason='not_reported')
    tokens = dict(reported.get('tokens') or {}) if reported.get('source') == 'provider_reported' else {}
    tokens = {k: tokens.get(k) for k in CATEGORIES}
    source = reported.get('source') if reported.get('source') in SOURCES else 'unavailable'
    semantics = reported.get('semantics') if source == 'provider_reported' else None
    scope = (semantics or {}).get('coverage_scope')
    reasons = [] if source != 'provider_reported' else (
        (['categories_not_reported'] if any(tokens[k] is None for k in CATEGORIES) else []) +
        ([] if scope == 'invocation_wide_reconciled' else ['scope_not_invocation_wide']))
    # `complete` = every category known AND the provider proved the aggregate covers the whole invocation.
    completeness = 'unavailable' if source != 'provider_reported' else ('partial' if reasons else 'complete')
    record = dict(
        schema_version=SCHEMA_VERSION, usage_id=usage_id(work_id, event.invocation_id), work_id=work_id,
        run_id=event.run_id, state_visit_id=event.state_visit_id, invocation_id=event.invocation_id,
        state=where.get('state'), runtime_state=where.get('runtime_state'), rung=where.get('rung'), agent=where.get('agent'),
        provider=where.get('provider'), configured_model=configured_model,
        resolved_model=reported.get('resolved_model') if source != 'not_applicable' else None,
        reported_model=reported.get('reported_model'), model_allocation=reported.get('model_allocation'),
        model_breakdown=(reported.get('model_breakdown') or [])[:16],
        model_breakdown_total=reported.get('model_breakdown_total') if source == 'provider_reported' else None,
        coverage_scope=scope, completeness_reasons=reasons, source=source, reason=reported.get('reason'),
        invalid_fields=list(reported.get('invalid_fields') or [])[:32], tokens=tokens, semantics=semantics,
        derived=dict(input_including_cache=_input_including_cache(tokens, semantics)), output_detail=_output_detail(reported, source),
        turns=reported.get('turns'),
        duration_ms=process.get('duration_ms'), completeness=completeness, observed_by=observed_by)
    return record


def write(root, record):
    directory = _directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory/'schema.json').exists():
        _publish(directory/'schema.json', dict(schema_version=SCHEMA_VERSION, record='invocation_usage', sources=list(SOURCES),
                                               categories=list(CATEGORIES)))
    path = directory/file_name(record['invocation_id'])
    if path.exists():
        return  # Finalized: never overwritten by a retry, a resume or a repeated recovery.
    data = json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    if len(data) > MAX_RECORD:
        raise ValueError('usage record exceeds its bound')
    _publish(path, record)


# --- Read API (read-only: no backfill, no recovery, no provider) -----------------------------------------------
FIRST_RECORDING_VERSION = (2, 6, 0)


def _recording_runtime(root):
    """Whether the work was created by a runtime that records usage (from its frozen baseline; unknown = no)."""
    from .logs import latest_record
    try:
        _, baseline = latest_record(Path(root)/'recovery', 'baseline')
        parts = tuple(int(p) for p in str(baseline.get('runtime_version')).split('.')[:3])
        return parts >= FIRST_RECORDING_VERSION
    except (AttributeError, TypeError, ValueError, OSError):
        return False


def read_usage(work_root, run=None, state=None, agent=None, provider=None, model=None, invocation=None):
    """(records, recorded): usage per invocation in canonical order; an invocation without a record is listed as
    `unavailable` with reason `not_recorded`, or `not_recorded_by_this_runtime_version` when the work has no usage
    store and was created by a runtime that did not record usage. Nothing is persisted."""
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
            if type(record) is dict and record.get('schema_version') == SCHEMA_VERSION and name == file_name(record.get('invocation_id', '')):
                stored[record['invocation_id']] = dict(record, recorded=True)
    order, located, _ = _invocations(root)
    out = []
    for event in order:
        record = stored.pop(event.invocation_id, None)
        if record is None:
            where = located.get(event.invocation_id) or {}
            record = dict(schema_version=SCHEMA_VERSION, usage_id=None, work_id=where.get('work_id'), run_id=event.run_id,
                          state_visit_id=event.state_visit_id, invocation_id=event.invocation_id,
                          state=where.get('state', event.state), runtime_state=event.state, rung=where.get('rung'),
                          agent=where.get('agent'), provider=where.get('provider'), configured_model=where.get('model'),
                          resolved_model=None, reported_model=None, model_allocation=None, model_breakdown=[],
                          model_breakdown_total=None, coverage_scope=None, completeness_reasons=[],
                          source='not_applicable' if where.get('provider') == 'command' else 'unavailable',
                          reason='verifier_command' if where.get('provider') == 'command' else
                          ('not_recorded' if recorded else 'not_recorded_by_this_runtime_version'),
                          invalid_fields=[], tokens={k: None for k in CATEGORIES}, semantics=None,
                          derived=dict(input_including_cache=None), output_detail=None, turns=None, duration_ms=None, completeness='unavailable',
                          observed_by=None, recorded=False)
        out.append(record)
    out.extend(stored.values())
    keep = []
    for r in out:
        if ((run is None or r['run_id'] == run) and (state is None or state in (r['state'], r['runtime_state']))
                and (agent is None or r['agent'] == agent) and (provider is None or r['provider'] == provider)
                and (model is None or model in (r['configured_model'], r['resolved_model'], r['reported_model'],
                                                *(m['model'] for m in r['model_breakdown'])))
                and (invocation is None or _match(r['invocation_id'], invocation))):
            keep.append(r)
    return keep, recorded


def attributed_model(record):
    """The model usage is attributed to: the provider-reported single model, else the configured model when the
    provider reported none, else `unallocated` (a multi-model or unusable breakdown is never split by guessing)."""
    allocation = record.get('model_allocation')
    if allocation == 'single_reported_model':
        return record['reported_model']
    if allocation in (None, 'not_reported'):
        return record.get('configured_model')
    return 'unallocated'


def summarize(records):
    """Counters for a set of records: coverage and per-category sums of KNOWN reported values only."""
    provider = [r for r in records if r['source'] != 'not_applicable']
    reported = [r for r in provider if r['source'] == 'provider_reported']
    sums = {}
    for k in CATEGORIES:
        known = [r['tokens'][k] for r in reported if r['tokens'][k] is not None]
        sums[k] = dict(reported_sum=sum(known) if known else None, invocations_reporting=len(known))
    derived = [r['derived']['input_including_cache'] for r in reported if r['derived']['input_including_cache'] is not None]
    scopes = {}
    for r in reported:
        scopes[r.get('coverage_scope') or 'unknown'] = scopes.get(r.get('coverage_scope') or 'unknown', 0) + 1
    trees = [r for r in reported if r.get('model_breakdown_total')]
    tree = {k: dict(reported_sum=sum(r['model_breakdown_total'][k] for r in trees if r['model_breakdown_total'][k] is not None)
                    if any(r['model_breakdown_total'][k] is not None for r in trees) else None,
                    invocations_reporting=len([r for r in trees if r['model_breakdown_total'][k] is not None])) for k in CATEGORIES}
    semantics = {r['semantics'].get('input_includes_cache') for r in reported if r.get('semantics')}
    if len(semantics) > 1 and sums['input']['reported_sum'] is not None:
        # Claude input excludes cache tokens, Codex input includes cached input: one summed "input" would be
        # ambiguous. Withheld; compare `input_including_cache` or group by provider instead.
        sums['input'] = dict(sums['input'], reported_sum=None, withheld='mixed_input_semantics')
    reasoning = [r['output_detail']['reasoning_output'] for r in reported
                 if (r.get('output_detail') or {}).get('reasoning_output') is not None]
    return dict(invocations=len(records), provider_invocations=len(provider), not_applicable=len(records)-len(provider),
                usage_reported=len(reported), usage_partial=len([r for r in reported if r['completeness'] == 'partial']),
                usage_unavailable=len(provider)-len(reported), tokens=sums,
                input_including_cache=dict(reported_sum=sum(derived) if derived else None, invocations_reporting=len(derived)),
                mixed_input_semantics=len(semantics) > 1,
                # A subset of `tokens.output` (Codex reasoning output); never added to it.
                output_detail=dict(reasoning_output=dict(reported_sum=sum(reasoning) if reasoning else None,
                                                         invocations_reporting=len(reasoning))),
                # How far `tokens` reaches: only `invocation_wide_reconciled` records are proven to cover every model call.
                coverage_scope=dict(sorted(scopes.items())),
                invocation_wide=bool(reported) and len(reported) == scopes.get('invocation_wide_reconciled', 0),
                # Separate figure: per-model whole-agent-tree usage (includes subagents); never added to `tokens`.
                agent_tree_by_model=tree)


def _key(record, dimension):
    return {'run': record['run_id'], 'state': record['state'], 'agent': record['agent'], 'provider': record['provider'],
            'model': attributed_model(record), 'invocation': record['invocation_id']}[dimension]


def aggregate(records):
    """Totals plus groups by every dimension; each invocation contributes once to each grouping."""
    groups = {}
    for dimension in DIMENSIONS:
        keys = list(dict.fromkeys(_key(r, dimension) for r in records))
        groups[dimension] = [dict(key=key, **summarize([r for r in records if _key(r, dimension) == key])) for key in keys]
    return dict(totals=summarize(records), groups=groups)
