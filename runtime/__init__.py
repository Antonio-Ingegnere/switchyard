"""Small synchronous control plane with optional event recording."""

from dataclasses import dataclass, field, replace
import time
from types import MappingProxyType
from typing import Mapping, Protocol
from uuid import uuid4


# Legacy adapter vocabulary; not the runtime semantic-outcome contract.
OUTCOMES = frozenset({"success", "failed", "error", "timeout", "cancelled"})

RESERVED_CONTROL_OUTCOMES = frozenset({'budget_exceeded'})


def _valid_outcome_name(value: object) -> bool:
    return type(value) is str and bool(value) and '\0' not in value


def _valid_executor_outcome(value: object) -> bool:
    return _valid_outcome_name(value) and value not in RESERVED_CONTROL_OUTCOMES


@dataclass(frozen=True, slots=True)
class WorkUnit:
    task: str

    def __post_init__(self):
        if type(self.task) is not str:
            raise TypeError("WorkUnit.task must be a string")


@dataclass(frozen=True, slots=True)
class RunContext:
    """Opaque control-plane provenance; never passed to executors."""

    correlation_id: str | None = None
    parent_run_id: str | None = None

    def __post_init__(self):
        for name in ('correlation_id', 'parent_run_id'):
            value = getattr(self, name)
            if value is not None:
                if type(value) is not str:
                    raise TypeError(f'{name} must be a string or None')
                if not value or '\0' in value:
                    raise ValueError(f'{name} must be nonempty and contain no NUL')


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    outcome: str


MAX_DIAGNOSTIC_BYTES = 4096


@dataclass(frozen=True, slots=True)
class DiagnosticPayload:
    utf8: bytes

    def __post_init__(self):
        if not _valid_payload(self):
            raise ValueError("diagnostics must be valid UTF-8 bytes within 4096 bytes")


def _valid_payload(payload: object) -> bool:
    if type(payload) is not DiagnosticPayload:
        return False
    data = getattr(payload, "utf8", None)
    if type(data) is not bytes or len(data) > MAX_DIAGNOSTIC_BYTES:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class DiagnosticOrigin:
    run_id: str
    state: str
    state_visit_id: str
    invocation_id: str


@dataclass(frozen=True, slots=True)
class DiagnosticInput:
    payload: DiagnosticPayload
    origin: DiagnosticOrigin


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    work_unit: WorkUnit
    diagnostics: DiagnosticInput | None = None


@dataclass(frozen=True, slots=True)
class InvocationResponse:
    result: ExecutorResult
    diagnostics: DiagnosticPayload | None = None


class Executor(Protocol):
    def run(self, request: InvocationRequest) -> InvocationResponse: ...


@dataclass(frozen=True, slots=True)
class InvocationBudgetScope:
    """Opaque shared invocation allowance within one run; validated with the flow."""

    name: str
    limit: int
    on_exceeded: str | None = None


@dataclass(frozen=True, slots=True)
class State:
    executor: str | None = None
    on: Mapping[str, str] = field(default_factory=dict)
    terminal: bool = False
    max_state_visits_per_run: int | None = None
    diagnostics_from_previous_max_bytes: int | None = None
    invocation_budgets: tuple[InvocationBudgetScope, ...] = ()
    state_visit_budget_exceeded: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "on", MappingProxyType(dict(self.on)))
        object.__setattr__(self, "invocation_budgets", tuple(self.invocation_budgets))


@dataclass(frozen=True, slots=True)
class Flow:
    initial: str
    states: Mapping[str, State]
    max_invocations_per_run: int | None = None
    max_invocations_exceeded: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "states", MappingProxyType(dict(self.states)))


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    run_id: str
    state: str | None = None
    state_visit_id: str | None = None
    invocation_id: str | None = None
    outcome: str | None = None
    target: str | None = None
    trigger_event: str | None = None
    metric: str | None = None
    limit: int | None = None
    observed: int | None = None
    occurred_at_unix_ns: int | None = None
    duration_ns: int | None = None
    correlation_id: str | None = None
    parent_run_id: str | None = None


class _Clock:
    def sample(self) -> tuple[int, int]:
        return time.time_ns(), time.monotonic_ns()


class _EventTiming:
    """Measure emission-to-emission intervals, before append and persistence."""

    def __init__(self):
        self.clock = _Clock()
        self.starts: dict[str, int] = {}

    def stamp(self, event: Event) -> Event:
        wall, monotonic = self.clock.sample()
        duration = None
        if event.kind in ('run_started', 'state_entered', 'invocation_started'):
            self.starts[event.kind] = monotonic
        elif event.kind in ('run_finished', 'run_error'):
            duration = monotonic - self.starts['run_started'] if 'run_started' in self.starts else None
        elif event.kind == 'state_exited':
            duration = monotonic - self.starts.pop('state_entered') if 'state_entered' in self.starts else None
        elif event.kind == 'invocation_finished':
            duration = monotonic - self.starts.pop('invocation_started')
        return replace(event, occurred_at_unix_ns=wall, duration_ns=duration)


class EventRecorder(Protocol):
    def record(self, event: Event) -> None: ...


@dataclass(frozen=True, slots=True)
class RunResult:
    terminal_state: str
    events: tuple[Event, ...]


class FlowValidationError(ValueError):
    pass


class ExecutionError(RuntimeError):
    def __init__(self, message: str, events: tuple[Event, ...]):
        super().__init__(message)
        self.events = events
        self.recorder_error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _BudgetRefusal:
    metric: str
    limit: int
    observed: int
    on_exceeded: str | None


def _validate_budget_target(target: str | None, flow: Flow, label: str) -> None:
    if target is not None and (type(target) is not str or target not in flow.states):
        raise FlowValidationError(f"invalid {label} target: {target!r}")


def validate(flow: Flow, executors: Mapping[str, Executor]) -> None:
    if not isinstance(flow.initial, str) or flow.initial not in flow.states:
        raise FlowValidationError("initial must reference an existing state")
    if not flow.states or any(not isinstance(name, str) or not name for name in flow.states):
        raise FlowValidationError("state names must be nonempty strings")
    run_limit = flow.max_invocations_per_run
    if run_limit is not None and (type(run_limit) is not int or run_limit < 0):
        raise FlowValidationError("invalid max_invocations_per_run")
    _validate_budget_target(flow.max_invocations_exceeded, flow, "run invocation budget")
    if flow.max_invocations_exceeded is not None and run_limit is None:
        raise FlowValidationError("run invocation budget route without limit")
    scope_declarations: dict[str, InvocationBudgetScope] = {}
    for name, state in flow.states.items():
        if type(state) is not State or type(state.terminal) is not bool:
            raise FlowValidationError(f"invalid state definition: {name}")
        limit = state.max_state_visits_per_run
        if limit is not None and (type(limit) is not int or limit < 0):
            raise FlowValidationError(f"invalid max_state_visits_per_run: {name}")
        _validate_budget_target(state.state_visit_budget_exceeded, flow, "state visit budget")
        if state.state_visit_budget_exceeded is not None and limit is None:
            raise FlowValidationError(f"state visit budget route without limit: {name}")
        seen_scopes: set[str] = set()
        for scope in state.invocation_budgets:
            if (type(scope) is not InvocationBudgetScope or not _valid_outcome_name(scope.name)
                    or type(scope.limit) is not int or scope.limit < 0):
                raise FlowValidationError(f"invalid invocation budget scope: {name}")
            if scope.name in seen_scopes:
                raise FlowValidationError(f"duplicate invocation budget scope: {name}: {scope.name!r}")
            seen_scopes.add(scope.name)
            _validate_budget_target(scope.on_exceeded, flow, "invocation scope budget")
            if scope.name in scope_declarations and scope_declarations[scope.name] != scope:
                raise FlowValidationError(f"conflicting invocation budget scope: {scope.name!r}")
            scope_declarations[scope.name] = scope
        input_limit = state.diagnostics_from_previous_max_bytes
        if input_limit is not None and (type(input_limit) is not int or not 0 <= input_limit <= MAX_DIAGNOSTIC_BYTES):
            raise FlowValidationError(f"invalid diagnostic input limit: {name}")
        if state.terminal:
            if state.executor is not None or state.on or limit is not None or input_limit is not None or state.invocation_budgets:
                raise FlowValidationError(f"terminal state has executor, transitions, or limit: {name}")
        elif (not isinstance(state.executor, str)
              or state.executor not in executors
              or not callable(getattr(executors[state.executor], "run", None))):
            raise FlowValidationError(f"invalid executor reference: {name}")
        for outcome, target in state.on.items():
            if not _valid_outcome_name(outcome):
                raise FlowValidationError(f"invalid outcome: {outcome}")
            if not isinstance(target, str) or target not in flow.states:
                raise FlowValidationError(f"unknown transition target: {target}")
    if not any(state.terminal for state in flow.states.values()):
        raise FlowValidationError("flow must contain a terminal state")

    if flow.max_invocations_exceeded is not None and not flow.states[flow.max_invocations_exceeded].terminal:
        raise FlowValidationError("run invocation budget target must be terminal")

    for name, state in flow.states.items():
        if "budget_exceeded" in state.on:
            if state.max_state_visits_per_run is None and not state.invocation_budgets and run_limit is None:
                raise FlowValidationError(f"budget handler on unlimited state: {name}")

    # A cycle must consume a finite admission allowance. Bounded semantic edges
    # consume one; refusal edges do not. Check the residual graph (including
    # unreachable components) for pure refusal and mixed unlimited-work cycles.
    remaining = {}
    for name, state in flow.states.items():
        targets = set()
        if state.max_state_visits_per_run is None:
            targets.update(state.on.values())
        elif "budget_exceeded" in state.on:
            targets.add(state.on["budget_exceeded"])
        if state.state_visit_budget_exceeded is not None:
            targets.add(state.state_visit_budget_exceeded)
        targets.update(scope.on_exceeded for scope in state.invocation_budgets
                       if scope.on_exceeded is not None)
        if not state.terminal and flow.max_invocations_exceeded is not None:
            targets.add(flow.max_invocations_exceeded)
        remaining[name] = targets
    incoming = dict.fromkeys(remaining, 0)
    for targets in remaining.values():
        for target in targets:
            incoming[target] += 1
    ready = [name for name, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        name = ready.pop()
        visited += 1
        for target in remaining[name]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(remaining):
        raise FlowValidationError("cycle without a bounded state admission (including budget refusal routes)")


def run(work_unit: WorkUnit, flow: Flow, executors: Mapping[str, Executor],
        *, recorder: EventRecorder | None = None,
        context: RunContext | None = None,
        resume_from: tuple[Event, ...] | None = None) -> RunResult:
    executors = dict(executors)
    validate(flow, executors)
    if type(work_unit) is not WorkUnit:
        raise TypeError("expected an immutable WorkUnit")
    if context is None:
        context = RunContext()
    elif type(context) is not RunContext:
        raise TypeError("expected an immutable RunContext or None")
    else:
        context = RunContext(context.correlation_id, context.parent_run_id)
    run_id = f"run:{uuid4()}"
    events: list[Event] = []
    if resume_from is not None:
        if (not resume_from or resume_from[0].kind != 'run_started'
            or resume_from[-1].kind != 'run_error' or resume_from[-1].invocation_id is None
            or resume_from[-1].outcome is not None
            or any(e.kind == 'run_finished' for e in resume_from)):
            raise ValueError('resume requires an interrupted invocation history')
        run_id = resume_from[0].run_id
        if any(e.run_id != run_id or (e.state is not None and e.state not in flow.states) for e in resume_from):
            raise ValueError('resume history does not match flow')
        context = RunContext(resume_from[0].correlation_id, resume_from[0].parent_run_id)
        events = list(resume_from)
    timing = _EventTiming()

    def emit(event: Event, failure: ExecutionError | None = None) -> None:
        event = replace(timing.stamp(event), correlation_id=context.correlation_id,
                        parent_run_id=context.parent_run_id)
        events.append(event)
        if failure is not None:
            failure.events = tuple(events)
        if recorder is not None:
            try:
                recorder.record(event)
            except Exception as exc:
                if failure is not None:
                    failure.recorder_error = exc
                    raise failure from failure.__cause__
                raise ExecutionError(f"recorder failed recording {event.kind}", tuple(events)) from exc

    def fail(message: str, event: Event, cause: Exception | None = None) -> None:
        failure = ExecutionError(message, tuple(events))
        failure.__cause__ = cause
        emit(event, failure)
        raise failure from cause

    def route_refusal(refusal: _BudgetRefusal, visit_id: str | None = None) -> str:
        nonlocal pending
        pending = None
        target = refusal.on_exceeded
        if target is None:
            target = state.on.get("budget_exceeded")
        emit(Event("budget_exceeded", run_id, current, visit_id,
                   target=refusal.on_exceeded, metric=refusal.metric,
                   limit=refusal.limit, observed=refusal.observed))
        if target is None:
            if visit_id is not None:
                emit(Event("state_exited", run_id, current, visit_id))
            fail(f"missing transition: {current}.budget_exceeded",
                 Event("run_error", run_id, current, trigger_event="budget_exceeded"))
        emit(Event("transition_selected", run_id, current, visit_id,
                   target=target, trigger_event="budget_exceeded"))
        if visit_id is not None:
            emit(Event("state_exited", run_id, current, visit_id))
        return target

    if resume_from is None:
        emit(Event("run_started", run_id))
    current = flow.initial
    visit = 0
    invocation = 0
    admitted_visits = dict.fromkeys(flow.states, 0)
    scope_usage = {scope.name: 0 for state in flow.states.values() for scope in state.invocation_budgets}
    pending: DiagnosticInput | None = None
    resuming_visit = resume_from is not None
    if resuming_visit:
        current = events[-1].state
        visit_id = events[-1].state_visit_id
        for event in events:
            if event.kind == 'state_entered':
                visit += 1
                admitted_visits[event.state] += 1
            elif event.kind == 'invocation_started':
                invocation += 1
                for scope in flow.states[event.state].invocation_budgets:
                    scope_usage[scope.name] += 1
        # Durations across process clocks cannot be reconstructed honestly.
        emit(Event('run_resumed', run_id, current, visit_id))
    while True:
        state = flow.states[current]
        if resuming_visit:
            resuming_visit = False
        else:
            limit = state.max_state_visits_per_run
            observed = admitted_visits[current]
            if limit is not None and observed >= limit:
                current = route_refusal(_BudgetRefusal("max_state_visits_per_run", limit, observed,
                                                       state.state_visit_budget_exceeded))
                continue
            input_limit = state.diagnostics_from_previous_max_bytes
            if input_limit is None or state.terminal:
                pending = None
            elif pending is not None and len(pending.payload.utf8) > input_limit:
                pending = None
                fail("diagnostic input exceeds target byte limit", Event("run_error", run_id, current))
            # Commit the visit after visit-limit and diagnostic admission checks.
            admitted_visits[current] += 1
            visit += 1
            visit_id = f"{run_id}:visit:{visit}"
            emit(Event("state_entered", run_id, current, visit_id))
        if state.terminal:
            emit(Event("state_exited", run_id, current, visit_id))
            emit(Event("run_finished", run_id, current))
            return RunResult(current, tuple(events))

        # Invocation admission is independent of the already committed visit.
        refusal = None
        limit = flow.max_invocations_per_run
        if limit is not None and invocation >= limit:
            refusal = _BudgetRefusal("max_invocations_per_run", limit, invocation,
                                     flow.max_invocations_exceeded)
        else:
            for scope in state.invocation_budgets:
                observed = scope_usage[scope.name]
                if observed >= scope.limit:
                    refusal = _BudgetRefusal(f"invocation_scope:{scope.name}",
                                             scope.limit, observed, scope.on_exceeded)
                    break
        if refusal is not None:
            current = route_refusal(refusal, visit_id)
            continue

        # Invocation capacity is committed before invocation_started, never rolled
        # back for executor or recorder failure. The visit is already committed.
        invocation += 1
        for scope in state.invocation_budgets:
            scope_usage[scope.name] += 1
        invocation_id = f"{run_id}:invocation:{invocation}"
        emit(Event("invocation_started", run_id, current, visit_id, invocation_id))
        request = InvocationRequest(work_unit, pending)
        pending = None
        try:
            response = executors[state.executor].run(request)
        except Exception as exc:
            fail(f"executor raised in state {current}",
                 Event("run_error", run_id, current, visit_id, invocation_id), exc)
        del request
        result = getattr(response, "result", None) if type(response) is InvocationResponse else None
        if type(result) is not ExecutorResult or not _valid_outcome_name(getattr(result, "outcome", None)):
            fail("executor returned a malformed result",
                 Event("run_error", run_id, current, visit_id, invocation_id))
        if result.outcome in RESERVED_CONTROL_OUTCOMES:
            fail(f"executor returned a malformed result: reserved control-plane outcome {result.outcome!r}",
                 Event("run_error", run_id, current, visit_id, invocation_id))
        if result.outcome not in state.on:
            declared = sorted(key for key in state.on if key not in RESERVED_CONTROL_OUTCOMES)
            fail(f"state {current!r} returned undeclared outcome {result.outcome!r}; declared outcomes: {declared!r}",
                 Event("run_error", run_id, current, visit_id, invocation_id))
        output = getattr(response, "diagnostics", False)
        if output is not None and not _valid_payload(output):
            fail("executor returned malformed diagnostic output", Event("run_error", run_id, current, visit_id, invocation_id))
        if output is not None:
            pending = DiagnosticInput(output, DiagnosticOrigin(run_id, current, visit_id, invocation_id))
        del response, output
        emit(Event("invocation_finished", run_id, current, visit_id, invocation_id, result.outcome))
        target = state.on[result.outcome]
        emit(Event("transition_selected", run_id, current, visit_id, invocation_id, result.outcome, target))
        emit(Event("state_exited", run_id, current, visit_id))
        current = target
