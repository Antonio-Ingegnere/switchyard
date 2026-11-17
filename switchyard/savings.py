"""ECONOMICS-006 (#2): continuity savings. A read-only report, over existing immutable records only, that keeps three
amounts apart for an adopted or transplanted work:

  A. historical source expenditure: frozen cost of the source executions behind a REUSED product (the original
     work's money, never the derived work's);
  B. actual new expenditure: the derived work's own frozen cost records;
  C. estimated avoided regeneration: a `historical_execution_proxy`, the known historical cost of the invocations
     PROVEN to have produced the reused products. A counterfactual comparison, never billed or proven savings.

Provenance: the derived work's own adoption record (decisions, artifact identities, implementation digest), then each
recorded source hop verified by durable identity (baseline digest, artifact manifest identity and bytes, implementation
digest) before any of its economics is read. A source invocation is attributable only when its live trace
`artifact_refs` name the exact reused artifact; any other invocation of the producing stage (failed attempts, earlier
QA rounds, superseded products) is reported as associated but unattributed, never guessed into the proxy.

Nothing is written, recovered, backfilled, repriced, fetched or executed; no catalog is loaded and no Git runs.
"""
import hashlib
import json
import os
from pathlib import Path

from .logs import latest_record, read_regular

REPORT_SCHEMA = 1       # The `work savings` JSON report; independent of every record schema.
MAX_DEPTH = 16          # Derivation hops followed from the derived work.
MAX_WORKS = 32          # Distinct works read in one report.
MAX_INVOCATIONS = 20000
MAX_MANIFEST = 8 << 20
MAX_ARTIFACT = 8 << 20
MAX_ISSUES = 100
REUSED, RERUN, INVALIDATED = 'REUSED', 'RERUN', 'INVALIDATED'
NO_PROVIDER_STAGES = ('REQUEST', 'SOURCE_RESOLUTION')  # Durable inputs: no model execution produced them.


class SavingsError(ValueError):
    pass


def _is_safe_dir(path):
    path = Path(path)
    try:
        return path.is_absolute() and not path.is_symlink() and path.is_dir() and path.resolve() == path
    except OSError:
        return False


class _Works:
    """Per-report cache: every work is read at most once, and at most MAX_WORKS works are read."""

    def __init__(self):
        self.cache, self.limited = {}, False

    def get(self, path):
        key = str(path)
        if key not in self.cache:
            if len(self.cache) >= MAX_WORKS:
                self.limited = True
                return dict(ok=False, reason='work_limit_reached', path=Path(path))
            self.cache[key] = _load(Path(path))
        return self.cache[key]


def _load(path):
    """Durable facts of one work: baseline identity, derivation record, operation phase. Bounded and read-only."""
    from .trace import work_identity
    facts = dict(ok=False, reason=None, path=path, directory=path.name, work_id=None, baseline_sha256=None, adoption=None,
                 phase=None, economics=None, usage=None, produced=None, manifest=None)
    if not _is_safe_dir(path):
        facts['reason'] = 'source_work_unavailable'
        return facts
    try:
        manifest, baseline = latest_record(path/'recovery', 'baseline')
        _, operation = latest_record(path/'recovery', 'operation')
    except (OSError, ValueError, TypeError, AttributeError):
        manifest = baseline = operation = None
    if type(baseline) is not dict or type((manifest or {}).get('sha256')) is not str:
        facts['phase'] = (operation or {}).get('phase') if type(operation) is dict else None
        facts['adoption'] = (operation or {}).get('adoption') if type(operation) is dict else None
        facts['reason'] = 'baseline_unavailable'
        return facts
    try:
        facts.update(work_id=work_identity(baseline, manifest['sha256']))
    except (KeyError, TypeError, AttributeError):
        facts['reason'] = 'baseline_unavailable'
        return facts
    facts.update(ok=True, baseline_sha256=manifest['sha256'], adoption=baseline.get('adoption'),
                 phase=(operation or {}).get('phase') if type(operation) is dict else None)
    return facts


def _manifest(facts):
    """{(name, version): (sha256, path)} of the work's artifact manifest (bounded, no symlinks)."""
    if facts['manifest'] is None:
        out = {}
        data = read_regular(facts['path']/'manifest.jsonl', MAX_MANIFEST)
        for line in (data or b'').decode('utf-8', 'replace').splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if type(record) is dict and record.get('kind') == 'artifact':
                out[(record.get('name'), record.get('version'))] = (record.get('sha256'), record.get('path'))
        facts['manifest'] = out
    return facts['manifest']


def _artifact_verified(facts, name, version, sha256):
    """The work holds exactly this artifact version (manifest identity) with exactly these bytes."""
    entry = _manifest(facts).get((name, version))
    if entry is None or entry[0] != sha256 or type(entry[1]) is not str or '..' in entry[1].split('/'):
        return False
    current = facts['path']
    for part in entry[1].split('/'):
        current = current/part
        if current.is_symlink():
            return False
    data = read_regular(current, MAX_ARTIFACT)
    return data is not None and hashlib.sha256(data).hexdigest() == sha256


def _produced(facts):
    """{(artifact, version, sha256): [invocation ids]} from live trace `artifact_refs`; invocations whose refs were
    not captured live (backfilled or untraced) are listed under None: their products are unknown."""
    if facts['produced'] is None:
        from .trace import trace_events
        out = {None: []}
        try:
            events = trace_events(facts['path'], event_type='invocation_finished')
        except (OSError, ValueError):
            events = []
        for event in events[:MAX_INVOCATIONS]:
            refs = (event.get('payload') or {}).get('artifact_refs')
            if refs is None:
                out[None].append(event.get('invocation_id'))
                continue
            for ref in refs:
                if type(ref) is dict and ref.get('kind') == 'artifact':
                    out.setdefault((ref.get('name'), ref.get('version'), ref.get('sha256')), []).append(event.get('invocation_id'))
        facts['produced'] = out
    return facts['produced']


def _economics(facts):
    """(cost rows, usage records by invocation) of one work, via the validated ECONOMICS-003/001 readers."""
    if facts['economics'] is None:
        from .analytics import read_work
        from .usage import read_usage
        _, rows, issues = read_work(facts['path'], MAX_INVOCATIONS)
        try:
            usage = {r['invocation_id']: r for r in read_usage(facts['path'])[0]}
        except (OSError, ValueError):
            usage = {}
        facts['economics'] = (rows, issues)
        facts['usage'] = usage
    return facts['economics']


def _adopted_digest(adoption):
    """Digest of the implementation a derived work adopted, relative to its own base (the transplant result when the
    base changed)."""
    if (adoption.get('transplant') or {}).get('result_digest'):
        return adoption['transplant']['result_digest']
    decision = next((d for d in (adoption.get('continuity') or {}).get('decisions') or [] if d.get('product') == 'implementation'), None)
    return ((decision or {}).get('implementation') or {}).get('digest')


def _source_digest(adoption):
    decision = next((d for d in (adoption.get('continuity') or {}).get('decisions') or [] if d.get('product') == 'implementation'), None)
    return ((decision or {}).get('implementation') or {}).get('digest')


def _attribute(works, adoption, decision, issues):
    """Trace one REUSED decision back to the work whose invocations produced it. Returns a bounded provenance dict:
    producer, verified hops, attributable invocation ids, and the producing stage's other invocations."""
    stage = decision.get('stage')
    identities = [(a.get('artifact'), a.get('version'), a.get('sha256')) for a in decision.get('artifacts') or [] if type(a) is dict]
    digest = ((decision.get('implementation') or {}).get('digest')) if decision.get('product') == 'implementation' else None
    out = dict(producer_work_id=None, producer_directory=None, hops=[], attribution='unavailable', reason=None,
               attributable=[], associated=[], producer=None)
    if stage in NO_PROVIDER_STAGES:
        out.update(attribution='not_applicable', reason='durable_input_no_model_execution')
        return out
    if not identities:
        out['reason'] = 'no_artifact_identity_recorded'
        return out
    record, seen = adoption, set()
    for _ in range(MAX_DEPTH):
        source = works.get(Path(record.get('from_work_root') or '/nonexistent'))
        hop = dict(work_id=record.get('from_work_id'), directory=source.get('directory'), verified=False, reason=None)
        out['hops'].append(hop)
        if not source['ok']:
            hop['reason'] = source['reason'] if source['reason'] != 'baseline_unavailable' else 'source_baseline_unavailable'
        elif source['baseline_sha256'] != record.get('from_baseline_sha256'):
            hop['reason'] = 'source_baseline_mismatch'
        elif record.get('from_work_id') not in (source['work_id'], source['directory']):
            hop['reason'] = 'source_identity_mismatch'
        elif not all(_artifact_verified(source, *identity) for identity in identities):
            hop['reason'] = 'source_artifact_identity_mismatch'
        if hop['reason']:
            out['reason'] = hop['reason']
            issues.append(dict(stage=stage, work=hop['work_id'], code=hop['reason']))
            return out
        hop.update(verified=True, work_id=source['work_id'])
        produced = _produced(source)
        found = [produced.get(identity) or [] for identity in identities]
        if all(found):
            ids = sorted({i for group in found for i in group})
            rows, _ = _economics(source)
            stage_rows = [r for r in rows if r['state'] == stage and r['status'] != 'not_applicable']
            out.update(producer_work_id=source['work_id'], producer_directory=source['directory'], attribution='artifact_refs',
                       attributable=ids, associated=sorted(r['invocation_id'] for r in stage_rows if r['invocation_id'] not in ids),
                       producer=source)
            return out
        upstream = source['adoption'] if type(source['adoption']) is dict else None
        reused = (upstream or {}).get('reused_artifacts') or {}
        if any(found) or upstream is None or not all((reused.get(name) or {}).get('sha256') == sha for name, _, sha in identities):
            out['reason'] = 'source_invocation_attribution_unavailable'  # Present and verified, but no invocation proven.
            issues.append(dict(stage=stage, work=source['work_id'], code=out['reason']))
            return out
        if digest is not None:
            if _adopted_digest(upstream) != digest:
                out['reason'] = 'implementation_identity_mismatch'  # The source changed the product it adopted.
                issues.append(dict(stage=stage, work=source['work_id'], code=out['reason']))
                return out
            digest = _source_digest(upstream)
        identities = [(name, reused[name].get('version'), sha) for name, _, sha in identities]
        seen.add(source['baseline_sha256'])
        if upstream.get('from_baseline_sha256') in seen:
            out['reason'] = 'derivation_cycle'
            issues.append(dict(stage=stage, work=source['work_id'], code=out['reason']))
            return out
        record = upstream
    out['reason'] = out['reason'] or 'derivation_depth_limit'
    issues.append(dict(stage=stage, work=None, code=out['reason']))
    return out


def _coverage(summary):
    if summary['complete_coverage']:
        return 'complete'
    return 'unavailable' if summary['known_estimated_subtotal'] is None else 'partial'


def _money(rows, blocked=False):
    from .analytics import summarize
    summary = summarize(rows, blocked)
    return dict(known_estimated_cost=summary['known_estimated_subtotal'], coverage=_coverage(summary),
                provider_invocations=summary['provider_invocations'], priced=summary['priced'],
                completely_priced=summary['completely_priced'], partial=summary['partial'], unavailable=summary['unavailable'],
                invalid=summary['invalid'], not_applicable=summary['not_applicable'], reasons=summary['reasons'])


def _tokens(records):
    """Known provider-reported tokens per provider (never summed across providers' different input semantics)."""
    from .usage import summarize
    out = []
    for provider in sorted({r.get('provider') or 'unknown' for r in records if r.get('source') != 'not_applicable'}):
        group = [r for r in records if (r.get('provider') or 'unknown') == provider]
        s = summarize(group)
        out.append(dict(provider=provider, usage_reported=s['usage_reported'], usage_unavailable=s['usage_unavailable'],
                        usage_partial=s['usage_partial'], tokens=s['tokens'], input_including_cache=s['input_including_cache'],
                        reasoning_output=s['output_detail']['reasoning_output'], coverage_scope=s['coverage_scope'],
                        invocation_wide=s['invocation_wide'], agent_tree_by_model=s['agent_tree_by_model']))
    return out


def _placeholder_usage(invocation_id):
    return dict(invocation_id=invocation_id, provider=None, source='unavailable', tokens=dict(input=None, output=None, cache_read=None,
                cache_write=None), derived=dict(input_including_cache=None), semantics=None, model_breakdown_total=None,
                completeness='unavailable', output_detail=None, coverage_scope=None)


def report(work_root):
    """The JSON-ready `work savings` report (schema REPORT_SCHEMA). Raises SavingsError when WORK is not a work."""
    root = Path(work_root).absolute()
    if root.is_symlink() or not root.is_dir() or not (root/'recovery'/'manifest.jsonl').is_file():
        raise SavingsError('not a work root (no recovery store): %s' % root.name)
    works = _Works()
    target = works.get(root.resolve())
    adoption = target['adoption'] if type(target['adoption']) is dict else None
    rows, own_issues = _economics(target) if target['ok'] else ([], [])
    actual = _money(rows, bool(own_issues))
    by_state = {}
    for r in rows:
        by_state.setdefault(r['state'] or 'unknown', []).append(r)
    actual['by_state'] = [dict(state=k, **_money(v, bool(own_issues))) for k, v in by_state.items()]
    actual['tokens'] = _tokens([target['usage'].get(r['invocation_id']) or _placeholder_usage(r['invocation_id']) for r in rows]) \
        if target['ok'] else []
    base = dict(schema_version=REPORT_SCHEMA, report='switchyard_continuity_savings', work_id=target['work_id'],
                directory=target['directory'], currency='USD', actual_new_work=actual, billed_savings=None)
    if adoption is None:
        return dict(base, derived=False, derivation=None, reuse=None, historical_source=None, avoided_regeneration=dict(
            basis='historical_execution_proxy', available=False, known_estimated_cost=None, coverage='not_applicable',
            reasons=['no_continuity_derivation'], billed_savings=None, actual_savings='not_applicable'),
            evidence=dict(flags=['no_continuity_derivation']), issues=[])
    issues = []
    decisions = (adoption.get('continuity') or {}).get('decisions') or []
    transplant = adoption.get('transplant') if type(adoption.get('transplant')) is dict else None
    derivation = dict(intent=adoption.get('intent', 'adopt'), complete=target['ok'] and target['phase'] != 'provisioning',
                      phase=target['phase'], source_work_id=adoption.get('from_work_id'),
                      source_baseline_sha256=adoption.get('from_baseline_sha256'), source_base_sha=adoption.get('from_base_sha'),
                      target_base_sha=(adoption.get('continuity') or {}).get('target_base_sha') or adoption.get('from_base_sha'),
                      base_changed=transplant is not None, implementation_source=adoption.get('implementation_source'),
                      implementation_identity=adoption.get('implementation_identity'),
                      source_product_digest=(adoption.get('source') or {}).get('product_digest'),
                      transplant=None if transplant is None else {k: transplant.get(k) for k in
                                                                  ('strategy', 'from_base', 'to_base', 'source_tree', 'result_tree',
                                                                   'result_digest', 'result_paths')})
    out_decisions, attributable, associated, producers = [], {}, {}, []
    for decision in decisions:
        entry = dict(stage=decision.get('stage'), product=decision.get('product'), action=decision.get('action'),
                     reason=decision.get('reason'),
                     artifacts=[{k: a.get(k) for k in ('artifact', 'version', 'sha256')} for a in decision.get('artifacts') or [] if type(a) is dict],
                     implementation_digest=((decision.get('implementation') or {}).get('digest')), provenance=None)
        if decision.get('action') == REUSED:
            p = _attribute(works, adoption, decision, issues)
            producer = p.pop('producer')
            entry['provenance'] = p
            if producer is not None:
                key = producer['baseline_sha256']
                if producer['work_id'] not in producers:
                    producers.append(producer['work_id'])
                prow = {r['invocation_id']: r for r in _economics(producer)[0]}
                for i in p['attributable']:  # Deduplicated across artifacts and decisions: (producer, invocation).
                    if i in prow:
                        attributable[(key, i)] = (prow[i], producer)
                for i in p['associated']:
                    if i in prow:
                        associated[(key, i)] = (prow[i], producer)
        out_decisions.append(entry)
    for k in attributable:
        associated.pop(k, None)
    reused = [d for d in out_decisions if d['action'] == REUSED]
    needs = [d for d in reused if d['provenance']['attribution'] != 'not_applicable']
    attributed = [d for d in needs if d['provenance']['attribution'] == 'artifact_refs']
    hops_verified = [d for d in reused if d['provenance']['attribution'] != 'not_applicable' and
                     d['provenance']['hops'] and all(h['verified'] for h in d['provenance']['hops'])]
    producer_issues = any(_economics(f)[1] for _, f in list(attributable.values()) + list(associated.values()))
    attributable_rows = [r for r, _ in attributable.values()]
    associated_rows = [r for r, _ in associated.values()]
    incomplete_attribution = len(attributed) < len(needs)
    proven = _money(attributable_rows, producer_issues or incomplete_attribution)
    stage_money = _money(attributable_rows + associated_rows, producer_issues or incomplete_attribution)
    unattributed = _money(associated_rows, producer_issues or incomplete_attribution) if associated_rows else None
    def usage_of(pairs):
        return [(f['usage'] or {}).get(r['invocation_id']) or _placeholder_usage(r['invocation_id']) for r, f in pairs]
    historical = dict(
        producers=producers, stage_associated=stage_money, attributable_to_reused_products=proven,
        associated_but_unattributed=unattributed, attributable_invocations=len(attributable_rows),
        unattributed_invocations=len(associated_rows),
        tokens=_tokens(usage_of(attributable.values())), basis='frozen_source_cost_records')
    flags = []
    flags.append('reuse_proven' if derivation['complete'] else 'reuse_incomplete')
    if len(hops_verified) < len(needs):
        flags.append('reuse_provenance_incomplete')
    if needs and not attributed:
        flags.append('source_economics_unavailable' if any(d['provenance']['reason'] in ('source_work_unavailable', 'source_baseline_unavailable')
                                                           for d in needs) else 'source_invocation_attribution_unavailable')
    if associated_rows or (attributed and incomplete_attribution):
        flags.append('source_invocation_attribution_uncertain')
    if attributable_rows and not any(r['status'] in ('complete', 'partial') for r in attributable_rows):
        flags.append('source_cost_unavailable')
    elif proven['coverage'] == 'partial' or any(r['status'] != 'complete' for r in attributable_rows):
        flags.append('source_pricing_partial')
    tokens = historical['tokens']
    if attributable_rows and not any(t['usage_reported'] for t in tokens):
        flags.append('source_usage_unavailable')
    if actual['coverage'] != 'complete':
        flags.append('derived_work_cost_' + actual['coverage'])
    available = proven['known_estimated_cost'] is not None
    flags.append('historical_proxy_available' if available else 'historical_proxy_unavailable')
    reasons = {d['provenance']['reason'] for d in needs if d['provenance']['reason']}
    if 'source_economics_unavailable' in flags or (attributable_rows and not available):
        reasons.add('source_economics_unavailable')  # Provenance may be proven; its money is not.
        reasons.update(x for r in attributable_rows for x in r['reasons'] if x in ('not_recorded', 'historical_pricing_not_captured',
                                                                                    'pricing_not_captured'))
    reasons = sorted(reasons)
    avoided = dict(basis='historical_execution_proxy', available=available, known_estimated_cost=proven['known_estimated_cost'],
                   coverage=proven['coverage'] if available else 'unavailable', reasons=reasons,
                   base_changed=derivation['base_changed'],
                   caveats=['counterfactual_not_proof_of_savings', 'regeneration_may_differ_in_provider_model_prompts_tokens_retries_prices',
                            'not_additive_across_derived_works'] + (['transplanted_to_a_different_base'] if derivation['base_changed'] else []),
                   billed_savings=None, actual_savings='not_established')
    return dict(base, derived=True, derivation=derivation,
                reuse=dict(decisions=out_decisions, reused_stages=len(reused),
                           rerun_stages=len([d for d in out_decisions if d['action'] == RERUN]),
                           invalidated_stages=len([d for d in out_decisions if d['action'] == INVALIDATED]),
                           reuse_proven=derivation['complete']),
                historical_source=historical, avoided_regeneration=avoided,
                evidence=dict(flags=flags, works_read=len(works.cache), work_limit_reached=works.limited),
                issues=issues[:MAX_ISSUES], issues_total=len(issues))
