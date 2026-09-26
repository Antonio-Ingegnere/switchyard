"""Pure analytics for DELIVERY-001 shapes; not generic runtime semantics."""
from dataclasses import dataclass, replace
from fractions import Fraction
import heapq
from typing import Iterable

from runtime import Event
from switchyard.observability import RunSummary, project_runs, summarize_runs


class DeliveryAnalyticsError(ValueError):
    """Contradictory observed delivery-specific provenance or decisions."""


@dataclass(frozen=True, slots=True)
class Ratio:
    numerator: int
    denominator: int
    unknown_count: int = 0

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in (self.numerator, self.denominator, self.unknown_count)):
            raise ValueError('ratio counts must be nonnegative integers')
        if self.unknown_count > self.denominator:
            raise ValueError('unknown count exceeds denominator')

    @property
    def value(self) -> Fraction | None:
        return Fraction(self.numerator, self.denominator) if self.denominator and not self.unknown_count else None


def _ratio(values) -> Ratio:
    values = tuple(values)
    return Ratio(sum(v is True for v in values), len(values), sum(v is None for v in values))


@dataclass(frozen=True, slots=True)
class DeliveryRunSummary:
    workflow: str
    run: RunSummary
    ended: bool
    verification_eligible: bool
    first_pass_verification: bool | None
    automatic_repair_used: bool | None
    repair_success: bool | None


@dataclass(frozen=True, slots=True)
class DeliveryLifecycleSummary:
    correlation_id: str
    run_ids: tuple[str, ...]
    deliver_run_count: int
    repair_run_count: int
    accept_run_count: int
    final_terminal: str | None
    accepted: bool
    qa_decision_count: int
    manual_qa_reopen_count: int
    manual_qa_pass_count: int
    automatic_repair_count: int
    rediagnosis_count: int
    needs_human_count: int
    total_invocation_count: int
    automation_duration_ns: int | None
    incomplete: bool

    def __post_init__(self):
        object.__setattr__(self, 'run_ids', tuple(self.run_ids))


@dataclass(frozen=True, slots=True)
class DeliveryAnalyticsSummary:
    runs: tuple[DeliveryRunSummary, ...]
    lifecycles: tuple[DeliveryLifecycleSummary, ...]
    uncorrelated_run_ids: tuple[str, ...]
    unrecognized_run_ids: tuple[str, ...]
    first_pass_verification_rate: Ratio
    automatic_repair_rate: Ratio
    automatic_repair_success_rate: Ratio
    rediagnosis_rate: Ratio
    needs_human_rate: Ratio
    manual_qa_reopen_rate: Ratio
    manual_qa_pass_rate: Ratio
    commit_blocked_rate: Ratio
    invocations_per_accepted_lifecycle: Ratio

    def __post_init__(self):
        for name in ('runs', 'lifecycles', 'uncorrelated_run_ids', 'unrecognized_run_ids'):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def lifecycle_count(self):
        return len(self.lifecycles)

    @property
    def accepted_lifecycle_count(self):
        return sum(lifecycle.accepted for lifecycle in self.lifecycles)

    @property
    def incomplete_lifecycle_count(self):
        return sum(lifecycle.incomplete for lifecycle in self.lifecycles)

    @property
    def uncorrelated_run_count(self):
        return len(self.uncorrelated_run_ids)

    @property
    def eligible_verification_run_count(self):
        return self.first_pass_verification_rate.denominator

    @property
    def first_pass_verification_count(self):
        return self.first_pass_verification_rate.numerator

    @property
    def automatic_repair_count(self):
        return self.automatic_repair_rate.numerator

    @property
    def automatic_repair_success_count(self):
        return self.automatic_repair_success_rate.numerator

    @property
    def rediagnosis_count(self):
        return self.rediagnosis_rate.numerator

    @property
    def needs_human_count(self):
        return self.needs_human_rate.numerator

    @property
    def qa_decision_count(self):
        return self.manual_qa_reopen_rate.denominator

    @property
    def manual_qa_reopen_count(self):
        return self.manual_qa_reopen_rate.numerator

    @property
    def manual_qa_pass_count(self):
        return self.manual_qa_pass_rate.numerator

    @property
    def accept_run_count(self):
        return self.commit_blocked_rate.denominator

    @property
    def commit_blocked_count(self):
        return self.commit_blocked_rate.numerator

    @property
    def total_invocations_for_accepted_lifecycles(self):
        return self.invocations_per_accepted_lifecycle.numerator

    @property
    def mean_invocations_per_accepted_lifecycle(self):
        return self.invocations_per_accepted_lifecycle.value


# Application-only signature of DELIVERY-001, not a generic workflow validator.
_INITIAL = {'ANALYZE': 'deliver', 'FRESH_DIAGNOSIS': 'repair', 'CHECK_WORKTREE': 'accept'}


def _shape(workflow):
    def on(success, failure, **extra):
        return {'success': success, 'failed': failure, 'error': failure,
                'timeout': failure, 'cancelled': failure, **extra}
    if workflow == 'accept':
        return {
            'CHECK_WORKTREE': on('CHECK_SCOPE', 'COMMIT_BLOCKED'),
            'CHECK_SCOPE': on('COMMIT', 'COMMIT_BLOCKED'),
            'COMMIT': on('ACCEPTED', 'COMMIT_BLOCKED'),
        }, {'ACCEPTED', 'COMMIT_BLOCKED'}
    first, implementation, failure = (('ANALYZE', 'IMPLEMENT', 'NEEDS_REDIAGNOSIS')
                                      if workflow == 'deliver' else ('FRESH_DIAGNOSIS', 'FIX', 'NEEDS_HUMAN'))
    return {
        first: on(implementation, failure),
        implementation: on('VERIFY', failure),
        'VERIFY': on('READY_FOR_MANUAL_QA', failure, failed='REPAIR_ONCE'),
        'REPAIR_ONCE': on('VERIFY_AGAIN', failure),
        'VERIFY_AGAIN': on('READY_FOR_MANUAL_QA', failure),
    }, {'READY_FOR_MANUAL_QA', failure}


def _classify(events):
    entered = [e.state for e in events if e.kind == 'state_entered']
    if not entered or entered[0] not in _INITIAL:
        return None
    if len(set(entered) & _INITIAL.keys()) > 1:
        raise DeliveryAnalyticsError('multiple delivery workflow initial states in one run')
    workflow = _INITIAL[entered[0]]
    graph, terminals = _shape(workflow)
    allowed = graph.keys() | terminals
    expected = entered[0]
    visited = set()
    for event in events:
        if event.state is not None and event.state not in allowed:
            return None
        if event.kind == 'state_entered':
            if event.state != expected or event.state in visited:
                return None
            visited.add(event.state)
            expected = None
        elif event.kind == 'invocation_started' and event.state not in graph:
            return None
        elif event.kind == 'transition_selected':
            if graph.get(event.state, {}).get(event.outcome) != event.target or event.trigger_event is not None:
                return None
            expected = event.target
        elif event.kind == 'budget_exceeded':
            # These acyclic presets cannot revisit the bounded repair state.
            return None
        elif event.kind == 'run_finished' and event.state not in terminals:
            return None
    return workflow


def _run_facts(events):
    summary, = summarize_runs(events)  # Public generic count/timing/provenance semantics.
    workflow = _classify(events)
    if workflow is None:
        return summary, None
    ended = events[-1].kind in ('run_finished', 'run_error')
    states = summary.state_visit_counts
    outcomes = {e.state: e.outcome for e in events if e.kind == 'invocation_finished'}
    eligible = workflow != 'accept' and 'VERIFY' in states
    attempted = 'REPAIR_ONCE' in states
    ready = summary.terminal_state == 'READY_FOR_MANUAL_QA'
    first = automatic = repaired = None
    if eligible:
        automatic = True if attempted else False if ended else None
        if outcomes.get('VERIFY') == 'success' and ready and not attempted:
            first = True
        elif attempted or ended or ('VERIFY' in outcomes and outcomes['VERIFY'] != 'success'):
            first = False
    if attempted:
        if outcomes.get('VERIFY_AGAIN') == 'success' and ready:
            repaired = True
        elif ended or ('VERIFY_AGAIN' in outcomes and outcomes['VERIFY_AGAIN'] != 'success'):
            repaired = False
    return summary, DeliveryRunSummary(workflow, summary, ended, eligible, first, automatic, repaired)


def _lifecycle(correlation_id, runs):
    by_id = {r.run.run_id: r for r in runs}
    positions = {r.run.run_id: i for i, r in enumerate(runs)}
    children = {run_id: [] for run_id in by_id}
    indegree = dict.fromkeys(by_id, 0)
    missing_parent = False
    for child in runs:
        parent = child.run.parent_run_id
        if parent in by_id:
            children[parent].append(child.run.run_id)
            indegree[child.run.run_id] += 1
            previous = by_id[parent]
            if child.workflow == 'accept' and previous.ended and previous.run.terminal_state != 'READY_FOR_MANUAL_QA':
                raise DeliveryAnalyticsError('accept parent did not finish ready for manual QA')
        elif parent is not None:
            missing_parent = True
    roots = [run_id for run_id, degree in indegree.items() if degree == 0]
    ready = [(positions[run_id], run_id) for run_id in roots]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _, run_id = heapq.heappop(ready)
        ordered.append(run_id)
        for child_id in children[run_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                heapq.heappush(ready, (positions[child_id], child_id))
    if len(ordered) != len(runs):
        raise DeliveryAnalyticsError('cyclic delivery parent relationships')
    decisions = reopens = passes = 0
    pending_qa = False
    for parent in runs:
        if parent.run.terminal_state != 'READY_FOR_MANUAL_QA':
            continue
        choices = {by_id[child].workflow for child in children[parent.run.run_id]} & {'repair', 'accept'}
        if len(choices) > 1:
            raise DeliveryAnalyticsError('conflicting manual QA decisions for one parent')
        if choices:
            decisions += 1
            reopens += 'repair' in choices
            passes += 'accept' in choices
        else:
            pending_qa = True
    leaves = [r for r in runs if not children[r.run.run_id]]
    durations = [r.run.run_duration_ns for r in runs]
    incomplete = (missing_parent or pending_qa or any(not r.ended for r in runs)
                  or len(roots) != 1 or any(len(ids) > 1 for ids in children.values()))
    if len(roots) == 1:
        root = by_id[roots[0]]
        incomplete |= root.workflow != 'deliver' or root.run.parent_run_id is not None
    return DeliveryLifecycleSummary(
        correlation_id, tuple(ordered),
        sum(r.workflow == 'deliver' for r in runs), sum(r.workflow == 'repair' for r in runs),
        sum(r.workflow == 'accept' for r in runs),
        leaves[0].run.terminal_state if len(leaves) == 1 else None,
        any(r.workflow == 'accept' and r.run.terminal_state == 'ACCEPTED' for r in runs),
        decisions, reopens, passes, sum(r.automatic_repair_used is True for r in runs),
        sum(r.run.terminal_state == 'NEEDS_REDIAGNOSIS' for r in runs),
        sum(r.run.terminal_state == 'NEEDS_HUMAN' for r in runs),
        sum(r.run.invocation_count for r in runs),
        sum(durations) if all(d is not None for d in durations) else None, bool(incomplete))


def _terminal_fact(run, terminal):
    return run.run.terminal_state == terminal if run.ended else None


def summarize_delivery_analytics(events: Iterable[Event]) -> DeliveryAnalyticsSummary:
    """Project DELIVERY-001 facts, preserving pending outcomes and absent provenance.

    Generic validation/grouping is delegated to public observability APIs. All
    results describe observed data, not unrecorded work or authenticated provenance.
    Ratio.value is unavailable for zero denominators or unresolved eligible results.
    """
    projected = project_runs(events, _run_facts)
    all_runs = {summary.run_id: summary for summary, _ in projected}
    recognized = tuple(facts for _, facts in projected if facts is not None)
    groups = {}
    for facts in recognized:
        current = facts.run
        parent = all_runs.get(current.parent_run_id)
        if (parent is not None and parent.correlation_id is not None and current.correlation_id is not None
                and parent.correlation_id != current.correlation_id):
            raise DeliveryAnalyticsError('parent belongs to another correlation lifecycle')
        if current.correlation_id is not None:
            groups.setdefault(current.correlation_id, []).append(facts)
    unresolved_groups = {summary.correlation_id for summary, facts in projected if facts is None}
    lifecycles = tuple(
        replace(_lifecycle(key, runs), incomplete=True, final_terminal=None)
        if key in unresolved_groups else _lifecycle(key, runs)
        for key, runs in groups.items())
    eligible = tuple(r for r in recognized if r.verification_eligible)
    attempts = tuple(r for r in recognized if r.automatic_repair_used is True)
    final_accepted = tuple(l for l in lifecycles if l.final_terminal == 'ACCEPTED')
    qa_decisions = sum(l.qa_decision_count for l in lifecycles)
    return DeliveryAnalyticsSummary(
        recognized, lifecycles,
        tuple(r.run.run_id for r in recognized if r.run.correlation_id is None),
        tuple(summary.run_id for summary, facts in projected if facts is None),
        _ratio(r.first_pass_verification for r in eligible),
        _ratio(r.automatic_repair_used for r in eligible),
        _ratio(r.repair_success for r in attempts),
        _ratio(_terminal_fact(r, 'NEEDS_REDIAGNOSIS') for r in recognized if r.workflow == 'deliver'),
        _ratio(_terminal_fact(r, 'NEEDS_HUMAN') for r in recognized if r.workflow == 'repair'),
        Ratio(sum(l.manual_qa_reopen_count for l in lifecycles), qa_decisions),
        Ratio(sum(l.manual_qa_pass_count for l in lifecycles), qa_decisions),
        _ratio(_terminal_fact(r, 'COMMIT_BLOCKED') for r in recognized if r.workflow == 'accept'),
        Ratio(sum(l.total_invocation_count for l in final_accepted), len(final_accepted)))


def summarize_delivery_lifecycles(events: Iterable[Event]) -> tuple[DeliveryLifecycleSummary, ...]:
    """Return only correlated delivery lifecycles; use full analytics for omitted IDs."""
    return summarize_delivery_analytics(events).lifecycles
