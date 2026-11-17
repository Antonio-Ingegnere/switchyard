"""ECONOMICS-004 (#2): versioned pricing registry, frozen per-invocation pricing snapshots and immutable cost
assessments.

An assessment is the ESTIMATED list-price cost of provider-REPORTED usage (switchyard.usage), never an invoiced or
billed amount. Observational: written by the diagnostics writer's lifecycle after the usage record, failure-isolated,
read by `work costs`; it never changes execution, retries, budgets or recovery.

Registry: the checked-in `pricing_catalog.json` (rates copied from the official provider pricing pages, with source
and verification date) plus an optional explicit user catalog named by SWITCHYARD_PRICING_CATALOG (same format; its
entries replace the built-in entry for the same provider and model). Nothing is fetched. Unknown models are never
priced, aliases are never guessed, and models billed per request by prompt length cannot be priced from aggregate
usage.

Reproducibility: when an invocation ends, the entries it needs are frozen in `<work>/economics/pricing/<inv>.json`
(rates, units, provenance, registry hashes, selection basis) TOGETHER WITH the chargeable quantities derived from
the usage (including Claude's in-memory 5m/1h cache-write split), and the assessment `<work>/economics/costs/<inv>.json`
references that snapshot. Both are immutable (exclusive-link publication). The live assessment and recovery assess
the same frozen quantities at the same frozen rates, so a crash between the two publications never changes the cost.
Without a contemporaneous snapshot recovery records `pricing_not_captured` and never applies the current catalog to
an earlier invocation.

Money: exact `Decimal` (an inexact operation raises); amounts are stored unrounded as decimal strings, aggregated
unrounded, and rounded only for display (ROUND_HALF_EVEN, 4 places).
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, Inexact, localcontext
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re

from .trace import _publish  # Bound at import: this store's publication fails independently of the others.

SCHEMA_VERSION = 1      # Pricing snapshot and cost assessment records; independent of every other schema.
CATALOG_SCHEMA = 1
CATALOG_ENV = 'SWITCHYARD_PRICING_CATALOG'
CURRENCY = 'USD'
UNIT = 'per_million_tokens'
MILLION = Decimal(1000000)
MAX_RECORD = 32768
MAX_ENTRIES = 512
FIRST_RECORDING_VERSION = (2, 7, 0)
CATEGORIES = {'claude': ('input', 'cache_write_5m', 'cache_write_1h', 'cache_read', 'output'),
              'codex': ('input', 'cache_read', 'output')}
# Scopes whose priced quantities cover every model call of the invocation, by the provider contract.
INVOCATION_WIDE = ('invocation_wide_reconciled', 'agent_tree_by_model')
DIMENSIONS = ('run', 'state', 'agent', 'provider', 'model', 'invocation')
_DECIMAL = re.compile(r'(0|[1-9][0-9]{0,6})(\.[0-9]{1,9})?')
_DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_URL = re.compile(r'https://[A-Za-z0-9./_#?=&%~-]{1,256}')


class CatalogError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def _sha(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def money(value):
    """A Decimal as an exact plain decimal string (no exponent, no float)."""
    text = format(value.normalize(), 'f') if value != 0 else '0'
    return text


def display(text, places=4):
    """Presentation rounding only: '$0.0942', '<$0.0001' for a positive amount below the display precision."""
    if text is None:
        return '—'
    value = Decimal(text)
    shown = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    if value > 0 and shown == 0:
        return '<$' + format(Decimal(1).scaleb(-places), 'f')
    return '${:,.{p}f}'.format(shown, p=places)


# --- Registry ---------------------------------------------------------------------------------------------------
def _day(value, field, optional=False):
    if value is None and optional:
        return None
    if type(value) is not str or not _DAY.fullmatch(value):
        raise CatalogError('invalid %s' % field)
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise CatalogError('invalid %s' % field) from None
    return value


def parse_catalog(data, origin):
    """Validated, fully expanded entries of one catalog document. Any defect rejects the whole catalog."""
    if type(data) is not dict or data.get('schema_version') != CATALOG_SCHEMA:
        raise CatalogError('unsupported catalog schema')
    if data.get('currency') != CURRENCY or data.get('unit') != UNIT:
        raise CatalogError('only USD per million tokens is supported')
    revision = data.get('revision')
    if type(revision) is not str or not _LABEL.fullmatch(revision):
        raise CatalogError('invalid revision')
    defaults = data.get('defaults') if type(data.get('defaults')) is dict else {}
    entries = data.get('entries')
    if type(entries) is not list or len(entries) > MAX_ENTRIES:
        raise CatalogError('invalid entries')
    out, seen = [], set()
    for raw in entries:
        if type(raw) is not dict:
            raise CatalogError('invalid entry')
        provider, model = raw.get('provider'), raw.get('model')
        if provider not in CATEGORIES or type(model) is not str or not _MODEL.fullmatch(model):
            raise CatalogError('invalid provider or model')
        def pick(key):
            value = raw.get(key, defaults.get(key))
            return value.get(provider) if key in ('sources', 'basis') and type(value) is dict else value
        start = _day(pick('effective_from'), 'effective_from')
        end = _day(pick('effective_until'), 'effective_until', optional=True)
        if end is not None and end <= start:
            raise CatalogError('empty applicability window')
        source = raw.get('source', pick('sources'))
        basis = pick('basis')
        if type(source) is not str or not _URL.fullmatch(source):
            raise CatalogError('a rate needs an https source')
        if type(basis) is not str or not _LABEL.fullmatch(basis):
            raise CatalogError('invalid basis')
        pricing = raw.get('pricing')
        entry = dict(provider=provider, model=model, pricing=pricing, currency=CURRENCY, unit=UNIT,
                     effective_from=start, effective_until=end, verified=_day(pick('verified'), 'verified'),
                     source=source, basis=basis, catalog_origin=origin, catalog_revision=revision, rates=None,
                     cache_write=None)
        if pricing == 'flat':
            rates = raw.get('rates')
            if type(rates) is not dict or set(rates) != set(CATEGORIES[provider]):
                raise CatalogError('rates must name exactly %s' % ', '.join(CATEGORIES[provider]))
            for key, value in rates.items():
                if type(value) is not str or not _DECIMAL.fullmatch(value):
                    raise CatalogError('a rate must be a non-negative decimal string')
            entry['rates'] = {k: rates[k] for k in CATEGORIES[provider]}
            if provider == 'codex':
                if raw.get('cache_write') != 'not_billed':
                    raise CatalogError('codex entries must state cache_write: not_billed')
                entry['cache_write'] = 'not_billed'
        elif pricing != 'per_request_context_tiered':
            raise CatalogError('invalid pricing kind')
        key = (provider, model, start)
        if key in seen:
            raise CatalogError('duplicate entry')
        seen.add(key)
        out.append(entry)
    return revision, out


class Registry:
    def __init__(self, documents):
        """documents: [(origin, parsed JSON)] in precedence order (later origins replace earlier ones per model)."""
        self.catalogs, self.entries = [], []
        for origin, data in documents:
            revision, entries = parse_catalog(data, origin)
            self.catalogs.append(dict(origin=origin, revision=revision, sha256=_sha(data)))
            self.entries.append((origin, entries))
        self.sha256 = _sha(self.catalogs)

    def select(self, provider, model, day):
        """(entry, None) or (None, reason). The latest origin that names the model decides; within it exactly one
        entry must be effective on `day`."""
        candidates = []
        for _, entries in self.entries:
            named = [e for e in entries if e['provider'] == provider and e['model'] == model]
            candidates = named or candidates
        if not candidates:
            return None, 'price_not_configured'
        effective = [e for e in candidates if e['effective_from'] <= day and (e['effective_until'] is None or day < e['effective_until'])]
        if not effective:
            return None, 'price_not_effective'
        if len(effective) > 1:
            return None, 'ambiguous_price'
        if effective[0]['pricing'] != 'flat':
            return None, 'tiered_pricing_unresolvable'
        return effective[0], None


def builtin_catalog():
    return json.loads(resources.files('switchyard').joinpath('pricing_catalog.json').read_text(encoding='utf-8'))


def load_registry(environ=None):
    environ = os.environ if environ is None else environ
    documents = [('builtin', builtin_catalog())]
    path = environ.get(CATALOG_ENV)
    if path:
        try:
            documents.append(('user_configuration', json.loads(Path(path).read_text(encoding='utf-8'))))
        except (OSError, ValueError) as exc:
            raise CatalogError('cannot read the configured pricing catalog') from exc
    return Registry(documents)


# --- Storage ----------------------------------------------------------------------------------------------------
def _directory(root, kind):
    return Path(root)/'economics'/kind


def file_name(invocation_id):
    return invocation_id.replace(':', '_')+'.json'


def cost_id(work_id, invocation_id):
    return hashlib.sha256('\0'.join((work_id, invocation_id, 'cost')).encode('utf-8')).hexdigest()[:32]


def _write(root, kind, record):
    directory = _directory(root, kind)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/file_name(record['invocation_id'])
    if path.exists():
        return
    if len(_canonical(record)) > MAX_RECORD:
        raise ValueError('economics record exceeds its bound')
    _publish(path, record)


def _read(root, kind, invocation_id):
    try:
        record = json.loads((_directory(root, kind)/file_name(invocation_id)).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if type(record) is dict and record.get('schema_version') == SCHEMA_VERSION else None


def snapshot_valid(snapshot):
    return type(snapshot) is dict and type(snapshot.get('quantities')) is dict \
        and snapshot.get('snapshot_id') == _sha({k: v for k, v in snapshot.items() if k != 'snapshot_id'})[:32]


def _usable(snapshot, usage):
    """The invocation's own valid snapshot, frozen from this usage record; otherwise None."""
    return snapshot if snapshot_valid(snapshot) and snapshot['quantities'].get('usage_id') == usage.get('usage_id') else None


# --- Quantities -------------------------------------------------------------------------------------------------
def _item(model, category, tokens, reason=None):
    return dict(model=model, category=category, tokens=tokens, reason=reason)


def _claude_items(model, tokens, detail):
    """Claude: input EXCLUDES cache tokens, so every category is a separate billed quantity. Cache writes are priced
    only with the reported 5m/1h split (different rates); otherwise that portion stays unpriced."""
    items = [_item(model, k, tokens[k], None if tokens[k] is not None else 'not_reported') for k in ('input', 'cache_read', 'output')]
    write = tokens['cache_write']
    if write is None:
        items.append(_item(model, 'cache_write', None, 'not_reported'))
    elif write > 0:
        if detail and detail.get('cache_write_split') == 'reported':
            items += [_item(model, 'cache_write_5m', detail['cache_write_5m']), _item(model, 'cache_write_1h', detail['cache_write_1h'])]
        else:
            items.append(_item(model, 'cache_write', write, 'cache_write_duration_not_reported'))
    return items


def _codex_items(model, tokens):
    """Codex: input INCLUDES cached input (uncached = input - cached, only when both are reported and consistent);
    reasoning output is inside output and never priced again."""
    total, cached = tokens['input'], tokens['cache_read']
    if total is None or cached is None:
        items = [_item(model, 'input', total, 'input_split_not_reported')]
    elif cached > total:
        items = [_item(model, 'input', total, 'cached_input_exceeds_input')]
    else:
        items = [_item(model, 'input', total - cached), _item(model, 'cache_read', cached)]
    items.append(_item(model, 'output', tokens['output'], None if tokens['output'] is not None else 'not_reported'))
    return items


def plan(usage):
    """(cost_scope, items, assumptions, blocking reason) for one usage record; the pricing-independent half."""
    if usage.get('source') == 'not_applicable' or usage.get('provider') == 'command':
        return None, [], [], 'not_applicable'
    if usage.get('source') != 'provider_reported':
        return None, [], [], 'usage_unavailable'
    tokens, provider = usage['tokens'], usage.get('provider')
    detail = usage.get('_pricing_detail')
    assumptions = []
    if provider == 'claude':
        if detail:
            modifiers = {k: detail.get(k) for k in ('service_tier', 'speed', 'inference_geo')}
            if modifiers['service_tier'] not in (None, 'standard') or modifiers['speed'] not in (None, 'standard') \
                    or modifiers['inference_geo'] not in (None, 'global'):
                return None, [], [], 'pricing_modifier_outside_catalog_basis'
            assumptions += ['%s_not_reported_assumed_standard' % k for k, v in modifiers.items() if v is None]
        else:
            assumptions.append('pricing_modifiers_not_captured_assumed_standard')
        breakdown, scope = usage.get('model_breakdown') or [], usage.get('coverage_scope')
        if scope == 'invocation_wide_reconciled' and usage.get('model_allocation') == 'single_reported_model':
            return scope, _claude_items(usage['reported_model'], tokens, detail), assumptions, None
        if breakdown:
            # Whole agent tree, per model at each model's own rate; never added to the top-level usage.
            total = usage.get('model_breakdown_total') or {}
            if any(total.get(k) is not None and tokens[k] is not None and total[k] < tokens[k] for k in tokens):
                return None, [], assumptions, 'model_breakdown_below_usage'
            items = []
            for entry in breakdown:
                items += _claude_items(entry['model'], {k: entry.get(k) for k in tokens}, None)
            return 'agent_tree_by_model', items, assumptions, None
        model = usage.get('reported_model') or usage.get('resolved_model')
        if not usage.get('reported_model'):
            assumptions.append('model_not_reported_requested_model_used')
        return scope, _claude_items(model, tokens, detail), assumptions, None
    if provider == 'codex':
        assumptions.append('model_not_reported_requested_model_used')
        assumptions.append('service_tier_not_reported_assumed_standard')
        return usage.get('coverage_scope'), _codex_items(usage.get('resolved_model'), tokens), assumptions, None
    return None, [], [], 'provider_not_priced'


def quantities(usage):
    """The chargeable quantities to freeze with the snapshot: everything `assess` needs from the usage, including
    in-memory detail (Claude's cache-write split, pricing modifiers) that the usage record does not hold."""
    scope, items, assumptions, blocked = plan(usage)
    return dict(usage_id=usage.get('usage_id'), cost_scope=scope, blocking_reason=blocked, items=items, assumptions=assumptions)


# Blocked reasons the durable usage record alone proves; nothing is frozen for them.
_DERIVABLE = ('not_applicable', 'usage_unavailable', 'provider_not_priced')


def _invocation_day(event):
    ns = getattr(event, 'occurred_at_unix_ns', None)
    return datetime.fromtimestamp(ns // 10**9, timezone.utc).strftime('%Y-%m-%d') if type(ns) is int else None


def freeze(work_id, invocation_id, provider, frozen, day, registry=None, registry_error=None):
    """The immutable pricing snapshot: the frozen chargeable quantities and the exact entries their models resolve
    to on `day`."""
    selected, unresolved, models = [], [], [i['model'] for i in frozen['items']]
    for model in sorted({m for m in models if m}):
        if registry is None or day is None:
            entry, reason = None, registry_error or 'invocation_time_unknown'
        else:
            entry, reason = registry.select(provider, model, day)
        if entry is not None:
            selected.append(entry)
        else:
            unresolved.append(dict(provider=provider, model=model, reason=reason))
    if any(not m for m in models):
        unresolved.append(dict(provider=provider, model=None, reason='model_not_identified'))
    body = dict(schema_version=SCHEMA_VERSION, record='pricing_snapshot', work_id=work_id, invocation_id=invocation_id,
                provider=provider, priced_on=day, currency=CURRENCY, unit=UNIT,
                registry=None if registry is None else dict(sha256=registry.sha256, catalogs=registry.catalogs),
                registry_error=registry_error,
                selection_basis=dict(date='invocation_end_utc_date', model='reported_model_else_exact_requested_model',
                                     precedence='user_configuration_over_builtin', aliases='never_resolved'),
                entries=selected, unresolved=unresolved, quantities=frozen)
    return dict(body, snapshot_id=_sha(body)[:32])


def assess(work_id, usage, snapshot, observed_by):
    """The deterministic cost assessment of one invocation: its snapshot's frozen quantities at its frozen rates.
    Without a snapshot, only what the usage record proves (never a price)."""
    frozen = (snapshot or {}).get('quantities') or quantities(usage)
    scope, items, assumptions, blocked = frozen['cost_scope'], frozen['items'], frozen['assumptions'], frozen['blocking_reason']
    lines, unpriced = [], []
    entries = {e['model']: e for e in (snapshot or {}).get('entries') or []}
    missing = {u['model']: u['reason'] for u in (snapshot or {}).get('unresolved') or []}
    if blocked is None and snapshot is None:
        blocked = 'pricing_not_captured'  # Never today's catalog for an earlier invocation.
    if blocked is None:
        with localcontext() as context:
            context.prec, context.traps[Inexact] = 60, True
            for item in items:
                entry = entries.get(item['model'])
                reason = item['reason'] or (None if entry is not None else missing.get(item['model'], 'price_not_configured'))
                if reason is not None or item['tokens'] is None:
                    unpriced.append(dict(item, reason=reason or 'not_reported'))
                    continue
                rate = Decimal(entry['rates'][item['category']])
                lines.append(dict(model=item['model'], category=item['category'], tokens=item['tokens'],
                                  rate=entry['rates'][item['category']], amount=money(Decimal(item['tokens']) * rate / MILLION)))
            amount = money(sum((Decimal(l['amount']) for l in lines), Decimal(0))) if lines else None
    else:
        amount = None
    reasons = list(dict.fromkeys(([blocked] if blocked else []) + [u['reason'] for u in unpriced] +
                                 (['scope_not_invocation_wide'] if lines and scope not in INVOCATION_WIDE else [])))
    if blocked == 'not_applicable':
        status = 'not_applicable'
    elif not lines:
        status = 'unavailable'
    else:
        status = 'complete' if not unpriced and scope in INVOCATION_WIDE else 'partial'
    return dict(schema_version=SCHEMA_VERSION, record='cost_assessment', cost_id=cost_id(work_id, usage['invocation_id']),
                work_id=work_id, run_id=usage.get('run_id'), state_visit_id=usage.get('state_visit_id'),
                invocation_id=usage['invocation_id'], state=usage.get('state'), runtime_state=usage.get('runtime_state'),
                agent=usage.get('agent'), provider=usage.get('provider'), configured_model=usage.get('configured_model'),
                usage_id=usage.get('usage_id'), usage_source=usage.get('source'), usage_reason=usage.get('reason'),
                pricing_snapshot_id=(snapshot or {}).get('snapshot_id'), kind='estimated_list_price_of_reported_usage',
                currency=CURRENCY, unit=UNIT, cost_scope=scope, status=status, reasons=reasons, assumptions=assumptions,
                lines=lines[:96], unpriced=unpriced[:96], estimated_amount=amount, arithmetic='exact_decimal_unrounded',
                observed_by=observed_by)


def publish(root, work_id, event, usage, pricing_detail, observed_by, environ=None):
    """Freeze (once) and assess (once) one ended invocation. Existing records are never rewritten."""
    if _read(root, 'costs', usage['invocation_id']) is not None:
        return
    frozen = quantities(dict(usage, _pricing_detail=pricing_detail))
    snapshot = None
    if frozen['blocking_reason'] not in _DERIVABLE:
        snapshot = _usable(_read(root, 'pricing', usage['invocation_id']), usage)  # A crash may have left the snapshot only.
        if snapshot is None:
            try:
                registry, error = load_registry(environ), None
            except (CatalogError, OSError, ValueError):
                registry, error = None, 'pricing_catalog_invalid'
            snapshot = freeze(work_id, usage['invocation_id'], usage['provider'], frozen, _invocation_day(event), registry, error)
            _write(root, 'pricing', snapshot)
            snapshot = _usable(_read(root, 'pricing', usage['invocation_id']), usage) or snapshot
    _write(root, 'costs', assess(work_id, usage, snapshot, observed_by))  # The same frozen inputs recovery would use.


def backfill(root, work_id, usage):
    """Recovery: assess from durable evidence only, i.e. the invocation's own frozen snapshot (quantities and rates)
    and usage record. No catalog is read and no quantity is reconstructed."""
    if _read(root, 'costs', usage['invocation_id']) is not None:
        return
    _write(root, 'costs', assess(work_id, usage, _usable(_read(root, 'pricing', usage['invocation_id']), usage), 'recovery_backfill'))


# --- Read API (read-only: no registry load, no network, no backfill) -----------------------------------------
def _recording_runtime(root):
    from .logs import latest_record
    try:
        _, baseline = latest_record(Path(root)/'recovery', 'baseline')
        return tuple(int(p) for p in str(baseline.get('runtime_version')).split('.')[:3]) >= FIRST_RECORDING_VERSION
    except (AttributeError, TypeError, ValueError, OSError):
        return False


def read_costs(work_root, run=None, state=None, agent=None, provider=None, model=None, invocation=None):
    """(records, recorded): one assessment per canonical invocation, each with its frozen snapshot's integrity.
    An invocation without an assessment is `unavailable`: `not_recorded`, or `historical_pricing_not_captured`
    for a work created before this runtime version recorded costs."""
    from .diagnostics import _invocations, _match
    root = Path(work_root)
    recorded = _directory(root, 'costs').is_dir() or _recording_runtime(root)
    order, located, _ = _invocations(root)
    out = []
    for event in order:
        record = _read(root, 'costs', event.invocation_id)
        if record is None:
            where = located.get(event.invocation_id) or {}
            command = where.get('provider') == 'command'
            record = dict(schema_version=SCHEMA_VERSION, record='cost_assessment', cost_id=None, work_id=where.get('work_id'),
                          run_id=event.run_id, state_visit_id=event.state_visit_id, invocation_id=event.invocation_id,
                          state=where.get('state', event.state), runtime_state=event.state, agent=where.get('agent'),
                          provider=where.get('provider'), configured_model=where.get('model'), usage_id=None, usage_source=None,
                          usage_reason=None, pricing_snapshot_id=None, kind='estimated_list_price_of_reported_usage',
                          currency=CURRENCY, unit=UNIT, cost_scope=None, status='not_applicable' if command else 'unavailable',
                          reasons=['not_applicable' if command else ('not_recorded' if recorded else 'historical_pricing_not_captured')],
                          assumptions=[], lines=[], unpriced=[], estimated_amount=None, arithmetic='exact_decimal_unrounded',
                          observed_by=None)
            integrity = None
        else:
            snapshot = _read(root, 'pricing', event.invocation_id) if record.get('pricing_snapshot_id') else None
            integrity = None if not record.get('pricing_snapshot_id') else (
                'verified' if snapshot_valid(snapshot) and snapshot['snapshot_id'] == record['pricing_snapshot_id'] else 'missing_or_altered')
        record = dict(record, recorded=record.get('cost_id') is not None, snapshot_integrity=integrity)
        out.append(record)
    keep = []
    for r in out:
        models = {r['configured_model'], *(l['model'] for l in r['lines']), *(u['model'] for u in r['unpriced'])}
        if ((run is None or r['run_id'] == run) and (state is None or state in (r['state'], r['runtime_state']))
                and (agent is None or r['agent'] == agent) and (provider is None or r['provider'] == provider)
                and (model is None or model in models) and (invocation is None or _match(r['invocation_id'], invocation))):
            keep.append(r)
    return keep, recorded


def read_snapshot(work_root, invocation_id):
    snapshot = _read(work_root, 'pricing', invocation_id)
    return snapshot if snapshot_valid(snapshot) else None


def _model_key(record):
    models = list(dict.fromkeys([l['model'] for l in record['lines']] + [u['model'] for u in record['unpriced'] if u['model']]))
    return models[0] if len(models) == 1 else ('multiple_models' if models else record.get('configured_model'))


def summarize(records):
    """Known estimated amount (exact, unrounded) and what it covers. Never a proven total unless every provider
    invocation is completely priced at invocation-wide scope."""
    provider = [r for r in records if r['status'] != 'not_applicable']
    priced = [r for r in provider if r['estimated_amount'] is not None]
    with localcontext() as context:
        context.prec, context.traps[Inexact] = 60, True
        known = money(sum((Decimal(r['estimated_amount']) for r in priced), Decimal(0))) if priced else None
    reasons = {}
    for r in provider:
        for reason in r['reasons']:
            reasons[reason] = reasons.get(reason, 0) + 1
    price_reasons = ('price_not_configured', 'price_not_effective', 'ambiguous_price', 'tiered_pricing_unresolvable',
                     'model_not_identified', 'pricing_catalog_invalid', 'pricing_not_captured',
                     'historical_pricing_not_captured', 'pricing_modifier_outside_catalog_basis')
    return dict(currency=CURRENCY, estimated_reported_usage_cost=known, invocations=len(records),
                provider_invocations=len(provider), not_applicable=len(records)-len(provider),
                with_usage=len([r for r in provider if r['usage_source'] == 'provider_reported']),
                priced=len(priced), complete=len([r for r in provider if r['status'] == 'complete']),
                partial=len([r for r in provider if r['status'] == 'partial']),
                unavailable=len([r for r in provider if r['status'] == 'unavailable']),
                price_unavailable=len([r for r in provider if any(x in price_reasons for x in r['reasons'])]),
                usage_unavailable=len([r for r in provider if r['usage_source'] not in (None, 'provider_reported')
                                       or 'usage_unavailable' in r['reasons']]),
                scope_incomplete=len([r for r in priced if r['cost_scope'] not in INVOCATION_WIDE]),
                invocation_wide_proven=bool(provider) and all(r['status'] == 'complete' for r in provider),
                reasons=dict(sorted(reasons.items())), billed_cost=None)


def _key(record, dimension):
    return {'run': record['run_id'], 'state': record['state'], 'agent': record['agent'], 'provider': record['provider'],
            'model': _model_key(record), 'invocation': record['invocation_id']}[dimension]


def aggregate(records):
    groups = {}
    for dimension in DIMENSIONS:
        keys = list(dict.fromkeys(_key(r, dimension) for r in records))
        groups[dimension] = [dict(key=key, **summarize([r for r in records if _key(r, dimension) == key])) for key in keys]
    return dict(totals=summarize(records), groups=groups)
