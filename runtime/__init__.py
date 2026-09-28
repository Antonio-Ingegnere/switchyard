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
class State:
    executor: str | None = None
    on: Mapping[str, str] = field(default_factory=dict)
    terminal: bool = False
    max_state_visits_per_run: int | None = None
    diagnostics_from_previous_max_bytes: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "on", MappingProxyType(dict(self.on)))


@dataclass(frozen=True, slots=True)
class Flow:
    initial: str
    states: Mapping[str, State]

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
            duration = monotonic - self.starts['run_started']
        elif event.kind == 'state_exited':
            duration = monotonic - self.starts.pop('state_entered')
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


def validate(flow: Flow, executors: Mapping[str, Executor]) -> None:
    if not isinstance(flow.initial, str) or flow.initial not in flow.states:
        raise FlowValidationError("initial must reference an existing state")
    if not flow.states or any(not isinstance(name, str) or not name for name in flow.states):
        raise FlowValidationError("state names must be nonempty strings")
    for name, state in flow.states.items():
        if type(state) is not State or type(state.terminal) is not bool:
            raise FlowValidationError(f"invalid state definition: {name}")
        limit = state.max_state_visits_per_run
        if limit is not None and (type(limit) is not int or limit < 0):
            raise FlowValidationError(f"invalid max_state_visits_per_run: {name}")
        input_limit = state.diagnostics_from_previous_max_bytes
        if input_limit is not None and (type(input_limit) is not int or not 0 <= input_limit <= MAX_DIAGNOSTIC_BYTES):
            raise FlowValidationError(f"invalid diagnostic input limit: {name}")
        if state.terminal:
            if state.executor is not None or state.on or limit is not None or input_limit is not None:
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

    for name, state in flow.states.items():
        if "budget_exceeded" in state.on:
            if state.max_state_visits_per_run is None:
                raise FlowValidationError(f"budget handler on unlimited state: {name}")

    # A cycle must consume a finite admission allowance. Bounded semantic edges
    # consume one; refusal edges do not. Check the residual graph (including
    # unreachable components) for pure refusal and mixed unlimited-work cycles.
    remaining = {
        name: ([state.on["budget_exceeded"]] if "budget_exceeded" in state.on else [])
        if state.max_state_visits_per_run is not None else list(state.on.values())
        for name, state in flow.states.items()
    }
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
        context: RunContext | None = None) -> RunResult:
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

    emit(Event("run_started", run_id))
    current = flow.initial
    visit = 0
    invocation = 0
    admitted_visits = dict.fromkeys(flow.states, 0)
    pending: DiagnosticInput | None = None
    while True:
        state = flow.states[current]
        limit = state.max_state_visits_per_run
        observed = admitted_visits[current]
        # A selected target is not an entered state. Refusal allocates no IDs.
        if limit is not None and observed >= limit:
            pending = None
            emit(Event(
                "budget_exceeded", run_id, current,
                metric="max_state_visits_per_run", limit=limit, observed=observed,
            ))
            if "budget_exceeded" not in state.on:
                fail(f"missing transition: {current}.budget_exceeded",
                     Event("run_error", run_id, current, trigger_event="budget_exceeded"))
            target = state.on["budget_exceeded"]
            emit(Event(
                "transition_selected", run_id, current,
                target=target, trigger_event="budget_exceeded",
            ))
            current = target
            continue
        input_limit = state.diagnostics_from_previous_max_bytes
        if input_limit is None or state.terminal:
            pending = None
        elif pending is not None and len(pending.payload.utf8) > input_limit:
            pending = None
            fail("diagnostic input exceeds target byte limit", Event("run_error", run_id, current))
        # Commit only after both budget and diagnostic checks pass.
        admitted_visits[current] += 1
        visit += 1
        visit_id = f"{run_id}:visit:{visit}"
        emit(Event("state_entered", run_id, current, visit_id))
        if state.terminal:
            emit(Event("state_exited", run_id, current, visit_id))
            emit(Event("run_finished", run_id, current))
            return RunResult(current, tuple(events))

        invocation += 1
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
