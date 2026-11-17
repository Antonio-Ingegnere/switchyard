"""ECONOMICS-003 (#2): historical cost analytics across works. Read-only, in-memory aggregation of the frozen cost
assessments of ECONOMICS-004 (`<work>/economics/costs/`), each verified against its frozen pricing snapshot.

Nothing is repriced, backfilled, recovered, locked, indexed or written; no catalog is loaded, and no provider, Git or
network is contacted. An amount that cannot be proven from intact records is excluded from the known subtotal and
reported, never replaced by zero.

Unit of accounting: one canonical invocation (`events.jsonl` `invocation_started`), counted at most once by its
durable identity. Time attribution: the UTC timestamp of the invocation's own end event (`invocation_finished`, or
the `run_error` that ended it) in the canonical log; never file times or the time of the report.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import stat

from . import economics
from .logs import latest_record, observed_status, read_regular

REPORT_SCHEMA = 1        # The `costs summary` JSON report; independent of every record schema.
MAX_WORKS = 1000
MAX_INVOCATIONS = 100000
MAX_EVENTS_BYTES = 64 << 20
MAX_RECORD = economics.MAX_RECORD
MAX_ISSUES = 200
TIME_DIMENSIONS = ('day', 'week', 'month')
DIMENSIONS = TIME_DIMENSIONS + ('work', 'state', 'agent', 'provider', 'model', 'status')
STATUSES = ('complete', 'partial', 'unavailable', 'not_applicable')
PRICE_REASONS = ('price_not_configured', 'price_not_effective', 'ambiguous_price', 'tiered_pricing_unresolvable',
                 'model_not_identified', 'pricing_catalog_invalid', 'pricing_modifier_outside_catalog_basis')
SNAPSHOT_REASONS = ('pricing_not_captured', 'historical_pricing_not_captured')
DELIVERY_OUTCOMES = ('accepted', 'awaiting_manual_qa', 'waiting_for_human', 'interrupted', 'rolled_back', 'active', 'other')
INVOCATION_OUTCOMES = ('succeeded', 'failed', 'unknown')


class ScopeError(ValueError):
    pass


# --- Discovery ----------------------------------------------------------------------------------------------------
def _is_work(path):
    """The work-location contract: a work root holds its recovery store (`recovery/manifest.jsonl`) or, for works
    older than the recovery store, its canonical `events.jsonl`. Regular files only, never through a link."""
    for relative in (('recovery', 'manifest.jsonl'), ('events.jsonl',)):
        current, ok = path, True
        for part in relative:
            current = current/part
            if current.is_symlink():
                ok = False
                break
        if ok:
            try:
                if stat.S_ISREG(os.lstat(current).st_mode):
                    return True
            except OSError:
                pass
    return False


def discover(works_root=None, works=()):
    """(work directories in deterministic order, discovery facts). `works_root`: a directory whose IMMEDIATE child
    directories are work roots (never recursive; symlinks and non-work entries are skipped and counted). `works`:
    explicit work roots, in the order given. Raises ScopeError for an invalid declared scope."""
    found, facts = [], dict(works_root=None if works_root is None else str(works_root), explicit_works=len(works),
                            symlinks_skipped=0, non_work_entries=0, work_limit=MAX_WORKS, works_not_read=0)
    if works_root is not None:
        root = Path(works_root)
        if root.is_symlink() or not root.is_dir():
            raise ScopeError('--works-root must be an existing directory (not a symlink)')
        if _is_work(root):
            raise ScopeError('--works-root is itself a work root; pass it with --work')
        for name in sorted(os.listdir(root)):
            child = root/name
            if child.is_symlink():
                facts['symlinks_skipped'] += 1
            elif child.is_dir() and _is_work(child):
                found.append(child)
            else:
                facts['non_work_entries'] += 1
    for work in works:
        path = Path(work)
        if path.is_symlink() or not path.is_dir() or not _is_work(path):
            raise ScopeError('--work must be an existing work root (not a symlink): %s' % path.name)
        found.append(path)
    unique = list(dict.fromkeys(p.resolve() for p in found))  # The same directory named twice is one directory.
    if len(unique) > MAX_WORKS:
        facts['works_not_read'] = len(unique) - MAX_WORKS
        unique = unique[:MAX_WORKS]
    return unique, facts


# --- Per-work reading (bounded, validated) ------------------------------------------------------------------------
def _json(data):
    def refuse(token):
        raise ValueError('non-standard number')
    return json.loads(data.decode('utf-8'), parse_float=lambda s: refuse(s), parse_constant=refuse)


def _bounded_json(path, limit=MAX_RECORD):
    """(value, problem): problem is None, 'missing', 'oversized_or_not_regular' or 'malformed'."""
    if not os.path.lexists(path):
        return None, 'missing'
    data = read_regular(path, limit)
    if data is None:
        return None, 'oversized_or_not_regular'
    try:
        return _json(data), None
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, 'malformed'


def _canonical_invocations(root):
    """(ordered invocation dicts, problem) from `events.jsonl` alone: identity, visit, runtime state and the UTC
    end timestamp of each invocation (None when the log proves none)."""
    from .observability import read_events
    path = root/'events.jsonl'
    if not os.path.lexists(path):
        return [], None
    try:
        info = os.lstat(path)
    except OSError:
        return [], 'events_unreadable'
    if not stat.S_ISREG(info.st_mode):
        return [], 'events_unreadable'
    if info.st_size > MAX_EVENTS_BYTES:
        return [], 'events_exceed_bound'
    order, ended = {}, {}
    try:
        for event in read_events(path):
            if event.kind == 'invocation_started' and event.invocation_id and event.invocation_id not in order:
                order[event.invocation_id] = dict(invocation_id=event.invocation_id, run_id=event.run_id,
                                                  state_visit_id=event.state_visit_id, runtime_state=event.state)
            elif event.kind in ('invocation_finished', 'run_error') and event.invocation_id in order \
                    and event.invocation_id not in ended:
                ended[event.invocation_id] = event.occurred_at_unix_ns
    except (ValueError, OSError):
        return [], 'events_unreadable'
    out = []
    for invocation_id, inv in order.items():
        ns = ended.get(invocation_id)
        inv['completed_at'] = None
        if type(ns) is int and ns > 0:
            try:
                inv['completed_at'] = datetime.fromtimestamp(ns // 10**9, timezone.utc).replace(microsecond=(ns // 1000) % 10**6)
            except (OverflowError, OSError, ValueError):
                pass  # Not a provable timestamp: date_unknown.
        out.append(inv)
    return out, None


def _decimal(text):
    if type(text) is not str or not text or text.strip() != text:
        raise ValueError('not a decimal string')
    value = Decimal(text)
    if not value.is_finite() or value < 0:
        raise ValueError('not a non-negative decimal')
    return value


def _verify_cost(record, work_id, snapshot):
    """None when the assessment is internally consistent and matches its frozen snapshot; else a bounded code.
    Only the frozen rates are used (verification, never repricing)."""
    if record.get('record') != 'cost_assessment' or record.get('status') not in STATUSES:
        return 'malformed_cost_record'
    if record.get('work_id') != work_id:
        return 'cost_work_identity_mismatch'
    if record.get('cost_id') != economics.cost_id(work_id, record['invocation_id']):
        return 'cost_identity_mismatch'
    if record.get('currency') != economics.CURRENCY or record.get('unit') != economics.UNIT:
        return 'unsupported_cost_currency_or_unit'
    lines, unpriced = record.get('lines'), record.get('unpriced')
    if type(lines) is not list or type(unpriced) is not list or type(record.get('reasons')) is not list:
        return 'malformed_cost_record'
    try:
        with localcontext() as context:
            context.prec, context.traps[Inexact] = 60, True
            total = Decimal(0)
            for line in lines:
                tokens = line.get('tokens') if type(line) is dict else None
                if type(tokens) is not int or tokens < 0 or type(line.get('category')) is not str:
                    return 'malformed_cost_record'
                amount = _decimal(line.get('amount'))
                if amount != Decimal(tokens) * _decimal(line.get('rate')) / economics.MILLION:
                    return 'cost_amount_inconsistent'
                total += amount
            stated = record.get('estimated_amount')
            if (stated is None) != (not lines) or (stated is not None and _decimal(stated) != total):
                return 'cost_amount_inconsistent'
    except (ValueError, TypeError, InvalidOperation, ArithmeticError):
        return 'malformed_cost_record'
    status = record['status']
    if (status == 'complete' and (not lines or unpriced)) or (status == 'partial' and not lines) or \
            (status in ('unavailable', 'not_applicable') and lines):
        return 'cost_status_inconsistent'
    snapshot_id = record.get('pricing_snapshot_id')
    if lines and not snapshot_id:
        return 'pricing_snapshot_missing'
    if snapshot_id:
        if snapshot is None:
            return 'pricing_snapshot_missing'
        if not economics.snapshot_valid(snapshot) or snapshot.get('snapshot_id') != snapshot_id \
                or snapshot.get('invocation_id') != record['invocation_id'] or snapshot.get('work_id') != work_id:
            return 'pricing_snapshot_altered_or_mismatched'
        entries = {e.get('model'): e for e in snapshot.get('entries') or [] if type(e) is dict}
        for line in lines:
            rates = (entries.get(line.get('model')) or {}).get('rates') or {}
            if rates.get(line['category']) != line['rate']:
                return 'cost_rate_not_in_snapshot'
    return None


def _outcome(diagnostic):
    """(invocation outcome, failure kind) from the invocation's recorded diagnostic; unknown without one."""
    if type(diagnostic) is not dict:
        return 'unknown', None
    from .diagnostics import failed
    if not failed(diagnostic):
        return 'succeeded', None
    charged = (diagnostic.get('result') or {}).get('semantic_invocation')
    return 'failed', 'infrastructure' if charged is False else 'semantic' if charged is True else None


def delivery_outcome(status):
    """Category of the CURRENT recorded lifecycle status (a live classification; it may change later)."""
    name = status.get('status')
    if name == 'ACCEPTED':
        return 'accepted'
    if name == 'READY_FOR_MANUAL_QA':
        return 'awaiting_manual_qa'
    if status.get('phase') in ('waiting_for_human', 'human_answer_ready') or name == 'WAITING_FOR_HUMAN':
        return 'waiting_for_human'
    if name == 'INTERRUPTED':
        return 'interrupted'
    if name == 'ROLLED_BACK':
        return 'rolled_back'
    if status.get('active') or name in ('RUNNING', 'REOPENED'):
        return 'active'
    return 'other'


def read_work(path, budget):
    """(work facts, invocation rows, issues) of one work directory; `budget` is the remaining invocation bound."""
    from .diagnostics import file_name as diagnostic_name
    from .trace import work_identity
    issues = []
    def issue(code, invocation_id=None):
        issues.append(dict(work=path.name, work_id=work_id, code=code, invocation_id=invocation_id))
    work_id = None
    try:
        manifest, baseline = latest_record(path/'recovery', 'baseline')
        if type(baseline) is dict and type((manifest or {}).get('sha256')) is str:
            work_id = work_identity(baseline, manifest['sha256'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        work_id = None
    status = observed_status(path)
    facts = dict(directory=path.name, work_id=work_id, status=status.get('status'), phase=status.get('phase'),
                 delivery_outcome=delivery_outcome(status), complete=True)
    if work_id is None:
        issue('work_identity_unavailable')
    invocations, problem = _canonical_invocations(path)
    if problem:
        issue(problem)
    known = {economics.file_name(inv['invocation_id']) for inv in invocations}
    if len(invocations) > budget:
        issue('invocation_limit_reached')
        invocations = invocations[:budget]
    costs_dir = path/'economics'/'costs'
    if costs_dir.is_dir() and not costs_dir.is_symlink() and not problem:
        for name in sorted(os.listdir(costs_dir)):
            if name.endswith('.json') and name not in known:
                issue('cost_record_without_canonical_invocation')  # Never counted: no canonical invocation proves it.
    recorded = costs_dir.is_dir() or economics._recording_runtime(path)
    rows = []
    for inv in invocations:
        invocation_id = inv['invocation_id']
        name = economics.file_name(invocation_id)
        diagnostic, _ = _bounded_json(path/'observability'/'diagnostics'/diagnostic_name(invocation_id))
        if type(diagnostic) is not dict or diagnostic.get('invocation_id') != invocation_id:
            diagnostic = None
        outcome, failure = _outcome(diagnostic)
        row = dict(directory=path.name, work_id=work_id, work_status=facts['status'], delivery_outcome=facts['delivery_outcome'],
                   invocation_id=invocation_id, run_id=inv['run_id'], state_visit_id=inv['state_visit_id'],
                   runtime_state=inv['runtime_state'], state=(diagnostic or {}).get('state'), agent=(diagnostic or {}).get('agent'),
                   provider=(diagnostic or {}).get('provider'), configured_model=(diagnostic or {}).get('model'),
                   completed_at=inv['completed_at'], invocation_outcome=outcome, failure_kind=failure,
                   status='unavailable', reasons=[], lines=[], unpriced=[], amount=None, pricing_snapshot_id=None,
                   pricing_revisions=[], cost_scope=None, usage_source=None, observed_by=None)
        record, problem = _bounded_json(costs_dir/name)
        if problem == 'missing':
            if row['provider'] == 'command':
                row.update(status='not_applicable', reasons=['not_applicable'])
            else:
                row['reasons'] = ['not_recorded' if recorded else 'historical_pricing_not_captured']
        elif problem or type(record) is not dict or record.get('invocation_id') != invocation_id:
            row.update(status='invalid', reasons=['malformed_cost_record'])
            issue('malformed_cost_record', invocation_id)
        elif record.get('schema_version') != economics.SCHEMA_VERSION:
            row.update(status='invalid', reasons=['unsupported_cost_schema'])
            issue('unsupported_cost_schema', invocation_id)
        else:
            snapshot = None
            if record.get('pricing_snapshot_id'):
                snapshot, _ = _bounded_json(path/'economics'/'pricing'/name)
            code = 'work_identity_unavailable' if work_id is None else _verify_cost(record, work_id, snapshot)
            for key in ('state', 'agent', 'provider', 'configured_model'):
                if record.get(key) is not None:
                    row[key] = record[key]
            if code is not None:
                row.update(status='invalid', reasons=[code])
                if code != 'work_identity_unavailable':
                    issue(code, invocation_id)
            else:
                revisions = {}
                for entry in (snapshot or {}).get('entries') or []:
                    revisions[(entry.get('catalog_origin'), entry.get('catalog_revision'))] = entry.get('source')
                row.update(status=record['status'], reasons=[r for r in record['reasons'] if type(r) is str],
                           lines=[dict(model=l.get('model'), category=l['category'], tokens=l['tokens'], rate=l['rate'],
                                       amount=l['amount']) for l in record['lines']],
                           unpriced=[dict(model=u.get('model'), category=u.get('category'), reason=u.get('reason'))
                                     for u in record['unpriced'] if type(u) is dict],
                           amount=record.get('estimated_amount'), pricing_snapshot_id=record.get('pricing_snapshot_id'),
                           pricing_revisions=[dict(catalog_origin=o, catalog_revision=r, source=s)
                                              for (o, r), s in sorted(revisions.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))],
                           cost_scope=record.get('cost_scope'), usage_source=record.get('usage_source'),
                           observed_by=record.get('observed_by'))
        rows.append(row)
    facts['complete'] = not issues
    return facts, rows, issues


# --- Report ---------------------------------------------------------------------------------------------------
def _day(value, flag):
    if value is None:
        return None
    try:
        if type(value) is not str or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ScopeError('%s must be a calendar date YYYY-MM-DD' % flag) from None


def period_key(moment, dimension):
    day = moment.date()
    if dimension == 'day':
        return day.isoformat()
    if dimension == 'week':
        year, week, _ = day.isocalendar()
        return '%04d-W%02d' % (year, week)
    return day.strftime('%Y-%m')


def _sum(values):
    with localcontext() as context:
        context.prec, context.traps[Inexact] = 60, True
        return economics.money(sum((Decimal(v) for v in values), Decimal(0)))


def _model(item):
    return item.get('model') or 'unallocated'


def _amount(row, model=None):
    """The row's known amount, or only its charge lines attributed to `model` (each line belongs to exactly one
    model group; a line without a model belongs to `unallocated`); None when unknown."""
    if model is None:
        return row['amount']
    lines = [l['amount'] for l in row['lines'] if _model(l) == model]
    return _sum(lines) if lines else None


def _models(row):
    """The models an invocation's frozen charge lines and unpriced items name; `unallocated` when none."""
    return list(dict.fromkeys(_model(item) for item in row['lines'] + row['unpriced'])) or ['unallocated']


def summarize(rows, blocked=False, model=None):
    """Known estimated subtotal and coverage of `rows`. Complete only when nothing blocks the scope and every
    provider invocation is completely priced; a known subtotal of an incomplete scope is never a total."""
    provider = [r for r in rows if r['status'] != 'not_applicable']
    amounts = [a for a in (_amount(r, model) for r in provider) if a is not None]
    complete = not blocked and all(r['status'] == 'complete' for r in provider)
    reasons = {}
    for r in provider:
        for reason in r['reasons']:
            reasons[reason] = reasons.get(reason, 0) + 1
    count = lambda test: len([r for r in provider if test(r)])
    return dict(known_estimated_subtotal=_sum(amounts) if amounts else ('0' if complete else None),
                complete_coverage=complete, provider_invocations=len(provider),
                priced=len(amounts),
                completely_priced=count(lambda r: r['status'] == 'complete'), partial=count(lambda r: r['status'] == 'partial'),
                unavailable=count(lambda r: r['status'] == 'unavailable'),
                invalid=count(lambda r: r['status'] == 'invalid'), conflicting=count(lambda r: r['status'] == 'conflicting'),
                usage_unavailable=count(lambda r: 'usage_unavailable' in r['reasons'] or r['usage_source'] not in (None, 'provider_reported')),
                price_unavailable=count(lambda r: any(x in PRICE_REASONS for x in r['reasons'])),
                pricing_not_captured=count(lambda r: any(x in SNAPSHOT_REASONS for x in r['reasons'])),
                not_recorded=count(lambda r: 'not_recorded' in r['reasons']),
                provider_unknown=count(lambda r: r['provider'] is None),
                not_applicable=len(rows)-len(provider), reasons=dict(sorted(reasons.items())))


def _key(row, dimension):
    if dimension in TIME_DIMENSIONS:
        return period_key(row['completed_at'], dimension)
    return {'work': row['work_key'], 'state': row['state'], 'agent': row['agent'],
            'provider': row['provider'], 'status': row['work_status']}[dimension] or 'unknown'


def _order(groups, dimension):
    if dimension in TIME_DIMENSIONS:
        return sorted(groups, key=lambda g: g['key'])  # Chronological (ISO keys sort chronologically).
    return sorted(groups, key=lambda g: (g['known_estimated_subtotal'] is None,
                                         -Decimal(g['known_estimated_subtotal'] or 0), g['key']))


def report(works_root=None, works=(), date_from=None, date_to=None, group_by='state', provider=None, model=None,
           agent=None, state=None, status=None, last=None):
    """The JSON-ready `costs summary` report (schema REPORT_SCHEMA). Raises ScopeError for an invalid request."""
    if group_by not in DIMENSIONS:
        raise ScopeError('unsupported --group-by')
    start, end = _day(date_from, '--from'), _day(date_to, '--to')
    if start and end and end < start:
        raise ScopeError('--to is before --from')
    lower = None if start is None else datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    upper = None if end is None else datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
    dated = lower is not None or upper is not None
    paths, discovery = discover(works_root, works)
    issues, works_out, rows, budget = [], [], [], MAX_INVOCATIONS
    if discovery['works_not_read']:
        issues.append(dict(work=None, work_id=None, code='work_limit_reached', invocation_id=None))
    for path in paths:
        if budget <= 0:
            discovery['works_not_read'] += 1
            continue
        facts, work_rows, work_issues = read_work(path, budget)
        budget -= len(work_rows)
        works_out.append(facts); rows += work_rows; issues += work_issues
    if discovery['works_not_read'] and not any(i['code'] == 'work_limit_reached' for i in issues):
        issues.append(dict(work=None, work_id=None, code='invocation_limit_reached', invocation_id=None))
    # Identity: two directories claiming one work, or one invocation in several works, are never resolved by choice.
    claims = {}
    for facts in works_out:
        if facts['work_id'] is not None:
            claims.setdefault(facts['work_id'], []).append(facts['directory'])
    for work_id, directories in sorted(claims.items()):
        if len(directories) > 1:
            issues.append(dict(work=None, work_id=work_id, code='duplicate_work_identity', invocation_id=None))
    owners = {}
    for row in rows:
        owners.setdefault(row['invocation_id'], set()).add(row['directory'])
        duplicate = len(claims.get(row['work_id']) or ()) > 1
        row['work_key'] = '%s@%s' % (row['work_id'], row['directory']) if duplicate else row['work_id'] or row['directory']
    for row in rows:
        if len(owners[row['invocation_id']]) > 1 or len(claims.get(row['work_id']) or ()) > 1:
            if row['status'] not in ('conflicting', 'not_applicable'):  # A command verifier carries no charge either way.
                row.update(status='conflicting', reasons=['conflicting_identity'], amount=None, lines=[], unpriced=[])
    for invocation_id in sorted(i for i, o in owners.items() if len(o) > 1):
        issues.append(dict(work=None, work_id=None, code='conflicting_invocation_identity', invocation_id=invocation_id))
    # Filters (work status, provider, agent, state, model); then the period.
    def keep(r):
        return ((status is None or r['work_status'] == status) and (provider is None or r['provider'] == provider)
                and (agent is None or r['agent'] == agent) and (state is None or state in (r['state'], r['runtime_state']))
                and (model is None or model in _models(r) or model == r['configured_model']))
    scoped = [r for r in rows if keep(r)]
    undated = [r for r in scoped if r['completed_at'] is None]
    in_period = [r for r in scoped if r['completed_at'] is not None and (lower is None or r['completed_at'] >= lower)
                 and (upper is None or r['completed_at'] < upper)]
    counted = in_period if dated else in_period + undated
    undated_provider = [r for r in undated if r['status'] != 'not_applicable']
    # Any issue (unreadable or damaged records, conflicts, limits) leaves the scope unproven, and so does an
    # undated provider invocation when a period is requested: it may belong to that period.
    blocked = bool(issues) or (dated and bool(undated_provider))
    totals = summarize(counted, blocked, model)
    dimension_rows = [r for r in counted if r['completed_at'] is not None] if group_by in TIME_DIMENSIONS else counted
    groups = []
    if group_by == 'model':
        keys = sorted({m for r in counted for m in _models(r)} if model is None else {model})
        for key in keys:
            members = [r for r in counted if key in _models(r) or (key == model and r['configured_model'] == model)]
            groups.append(dict(key=key, **summarize(members, blocked, key)))
    else:
        keys = sorted({_key(r, group_by) for r in dimension_rows})
        for key in keys:
            members = [r for r in dimension_rows if _key(r, group_by) == key]
            group = dict(key=key, **summarize(members, blocked, model))
            if group_by == 'work':
                first = members[0]
                group.update(work_id=first['work_id'], directory=first['directory'], status=first['work_status'],
                             delivery_outcome=first['delivery_outcome'],
                             work_issues=len([i for i in issues if i['work'] == first['directory']]))
            groups.append(group)
    if group_by == 'work':  # Works without invocations in scope are still observed (and listed when unfiltered).
        listed = {g['directory'] for g in groups}
        for facts in works_out:
            if facts['directory'] not in listed and not dated and provider is agent is state is model is None \
                    and (status is None or facts['status'] == status):
                groups.append(dict(key=facts['work_id'] or facts['directory'], **summarize([], blocked),
                                   work_id=facts['work_id'], directory=facts['directory'], status=facts['status'],
                                   delivery_outcome=facts['delivery_outcome'],
                                   work_issues=len([i for i in issues if i['work'] == facts['directory']])))
    groups = _order(groups, group_by)
    ranking = None
    if last is not None and last < 1:
        raise ScopeError('--last must be at least 1')
    def known_sum(selected):
        known = [g['known_estimated_subtotal'] for g in selected if g['known_estimated_subtotal'] is not None]
        return _sum(known) if known else None
    undated_known = None
    if group_by in TIME_DIMENSIONS and not dated:
        undated_known = summarize(undated, True, model)['known_estimated_subtotal']
    # Integrity is checked over ALL groups: each counted amount (or charge line) sits in exactly one group, so the
    # full accounting reconciles whether or not --last then limits which groups are displayed.
    parts = [x for x in (known_sum(groups), undated_known) if x is not None]
    expected = totals['known_estimated_subtotal']
    reconciles = expected in (None, '0') if not parts else expected is not None and Decimal(_sum(parts)) == Decimal(expected)
    all_groups = len(groups)
    if last is not None:
        displayed = groups[-last:] if group_by in TIME_DIMENSIONS else groups[:last]
        omitted = groups[:-last] if group_by in TIME_DIMENSIONS else groups[last:]
        ranking = None if group_by in TIME_DIMENSIONS else 'top_%s_by_known_estimated_spend' % group_by
    else:
        displayed, omitted = groups, []
    full_known = known_sum(groups)
    groups = displayed
    delivery = []
    for outcome in DELIVERY_OUTCOMES:
        members = [r for r in counted if r['delivery_outcome'] == outcome]
        observed = [w for w in works_out if w['delivery_outcome'] == outcome and (status is None or w['status'] == status)]
        if members or observed:
            delivery.append(dict(outcome=outcome, works=len(observed), **summarize(members, blocked, model)))
    invocation = []
    for outcome in INVOCATION_OUTCOMES:
        members = [r for r in counted if r['invocation_outcome'] == outcome and r['status'] != 'not_applicable']
        if members:
            entry = dict(outcome=outcome, **summarize(members, blocked, model))
            if outcome == 'failed':
                entry['infrastructure_failures'] = len([r for r in members if r['failure_kind'] == 'infrastructure'])
                entry['semantic_failures'] = len([r for r in members if r['failure_kind'] == 'semantic'])
            invocation.append(entry)
    revisions = {}
    for r in counted:  # Provenance of the counted amounts, for drill-down: the frozen catalog revisions they used.
        for rev in r['pricing_revisions']:
            key = (str(rev['catalog_origin']), str(rev['catalog_revision']), str(rev['source']))
            revisions[key] = revisions.get(key, 0) + 1
    return dict(
        schema_version=REPORT_SCHEMA, report='switchyard_cost_summary', kind='estimated_list_price_of_reported_usage',
        currency=economics.CURRENCY,
        period=dict(**{'from': None if start is None else start.isoformat()}, to=None if end is None else end.isoformat(),
                    timezone='UTC', boundary='from_inclusive_to_inclusive_calendar_date',
                    interval=dict(start=None if lower is None else lower.strftime('%Y-%m-%dT%H:%M:%SZ'),
                                  end_exclusive=None if upper is None else upper.strftime('%Y-%m-%dT%H:%M:%SZ')),
                    attribution='invocation_end_event_utc', all_time=not dated),
        scope=discovery, filters=dict(provider=provider, model=model, agent=agent, state=state, status=status),
        group_by=group_by, last=last, ranking=ranking,
        known_estimated_subtotal=totals['known_estimated_subtotal'], billed_cost=None,
        coverage=dict({k: v for k, v in totals.items() if k not in ('known_estimated_subtotal', 'complete_coverage')},
                      complete=totals['complete_coverage'], works_observed=len(works_out),
                      works_with_issues=len({i['work'] for i in issues if i['work']}),
                      date_unknown=len([r for r in undated if r['status'] != 'not_applicable'])),
        date_unknown=dict(provider_invocations=len(undated_provider),
                          known_estimated_subtotal=summarize(undated, True, model)['known_estimated_subtotal'],
                          included_in_subtotal=not dated),
        groups=groups,
        reconciliation=dict(groups_known_sum=full_known, displayed_groups_known_sum=known_sum(displayed),
                            omitted_groups_known_sum=known_sum(omitted), groups_total=all_groups,
                            groups_displayed=len(displayed), groups_omitted=len(omitted),
                            undated_known_sum=undated_known, reconciles_with_subtotal=reconciles),
        delivery_outcomes=delivery, delivery_outcome_basis='current_recorded_status_live_classification',
        invocation_outcomes=invocation, invocation_outcome_basis='recorded_invocation_diagnostic',
        pricing_revisions=[dict(catalog_origin=o, catalog_revision=v, source=u, invocations=n)
                           for (o, v, u), n in sorted(revisions.items())],
        issues=issues[:MAX_ISSUES], issues_total=len(issues), issues_truncated=len(issues) > MAX_ISSUES)
