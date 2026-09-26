"""Strict canonical-event reading and generic, offline run summaries."""

from collections import Counter
from dataclasses import dataclass, field, fields
import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from runtime import Event, OUTCOMES


class ObservabilityError(ValueError):
    """An invalid canonical record or structurally inconsistent event history."""


_FIELDS = frozenset(field.name for field in fields(Event))
_TIMING = {'occurred_at_unix_ns', 'duration_ns'}
_LEGACY_FIELDS = _FIELDS - _TIMING
_BASE = {'kind', 'run_id'}
_DURATION_KINDS = {'invocation_finished', 'state_exited', 'run_finished', 'run_error'}
_VISIT = {'state', 'state_visit_id'}
_INVOCATION = _VISIT | {'invocation_id'}
_SHAPES = {
    'run_started': [set()],
    'state_entered': [_VISIT],
    'state_exited': [_VISIT],
    'invocation_started': [_INVOCATION],
    'invocation_finished': [_INVOCATION | {'outcome'}],
    'transition_selected': [_INVOCATION | {'outcome', 'target'},
                            {'state', 'target', 'trigger_event'}],
    'budget_exceeded': [{'state', 'metric', 'limit', 'observed'}],
    'run_finished': [{'state'}],
    'run_error': [{'state'}, {'state', 'trigger_event'}, _INVOCATION,
                  _INVOCATION | {'outcome'}],
}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ObservabilityError(message)


def _validate(event: Event) -> None:
    _check(type(event) is Event, 'expected canonical Event')
    for name in _FIELDS:
        value = getattr(event, name)
        if name == 'occurred_at_unix_ns':
            _check(value is None or type(value) is int, 'invalid occurrence timestamp type')
        elif name in ('limit', 'observed', 'duration_ns'):
            _check(value is None or (type(value) is int and value >= 0),
                   'invalid counter field type or value')
        else:
            _check(value is None or (type(value) is str and bool(value)),
                   'expected nonempty string or null')
    _check(event.run_id is not None and event.kind in _SHAPES, 'unknown event kind or missing run ID')
    present = {name for name in _LEGACY_FIELDS - _BASE if getattr(event, name) is not None}
    _check(present in _SHAPES[event.kind], 'invalid fields for event kind')
    _check(event.duration_ns is None or event.kind in _DURATION_KINDS,
           'duration on event without an elapsed interval')
    _check(event.outcome is None or event.outcome in OUTCOMES, 'unknown outcome')
    _check(event.trigger_event is None or event.trigger_event == 'budget_exceeded', 'unknown trigger event')
    if event.kind == 'budget_exceeded':
        _check(event.metric == 'max_state_visits_per_run', 'unknown budget metric')
        _check(event.observed >= event.limit, 'budget observation below limit')


def _object(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result, 'duplicate JSON field')
        result[key] = value
    return result


def _constant(value):
    raise ObservabilityError('nonstandard JSON constant')


def read_events(path: str | Path) -> Iterator[Event]:
    """Yield validated events in file order; exhaust to validate the entire file.

    Records must be UTF-8 JSON objects terminated by LF. Filesystem errors remain
    OSError; malformed records raise ObservabilityError with a 1-based line number.
    This reader checks records, while summarize_runs also checks stream structure.
    """
    with Path(path).open('rb') as stream:
        for number, line in enumerate(stream, 1):
            try:
                _check(line.endswith(b'\n'), 'unterminated JSONL record')
                record = json.loads(line.decode('utf-8'), object_pairs_hook=_object,
                                    parse_constant=_constant)
                _check(type(record) is dict, 'expected JSON object')
                _check(_LEGACY_FIELDS <= record.keys() <= _FIELDS, 'missing or unexpected Event fields')
                event = Event(**record)
                _validate(event)
            except (ValueError, RecursionError) as exc:
                # Do not expose source text from JSON/Unicode exceptions.
                detail = str(exc) if isinstance(exc, ObservabilityError) else 'invalid JSON or UTF-8'
                raise ObservabilityError(f'line {number}: {detail}') from None
            yield event


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    terminal_state: str | None
    state_visit_counts: Mapping[str, int]
    invocation_count: int
    invocation_counts_by_state: Mapping[str, int]
    outcome_counts: Mapping[str, int]
    transition_count: int
    budget_exceeded_count: int
    completed_normally: bool
    run_duration_ns: int | None = None
    invocation_duration_ns_total: int | None = None
    state_duration_ns: Mapping[str, int | None] = field(default_factory=dict)

    def __post_init__(self):
        for name in ('state_visit_counts', 'invocation_counts_by_state', 'outcome_counts', 'state_duration_ns'):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


class _Run:
    def __init__(self, run_id):
        self.run_id = run_id
        self.started = False
        self.ended = False
        self.terminal = None
        self.visits = Counter()
        self.invocations = Counter()
        self.outcomes = Counter()
        self.transitions = 0
        self.budgets = 0
        self.visit_ids = set()
        self.invocation_ids = set()
        self.active = None
        self.invocation = None
        self.outcome = None
        self.transitioned = False
        self.budget = None
        self.closed_terminal = None
        self.timed_start = False
        self.run_duration = None
        self.invocation_duration = 0
        self.timed_invocations = 0
        self.state_durations = Counter()
        self.timed_visits = Counter()

    def consume(self, event):
        kind = event.kind
        _check(not self.ended, 'event after run end')
        if not self.started:
            _check(kind == 'run_started', 'run must begin with run_started')
            self.started = True
            self.timed_start = event.occurred_at_unix_ns is not None
            return
        _check(kind != 'run_started', 'duplicate run_started')
        _check(self.closed_terminal is None or kind == 'run_finished',
               'expected completion after terminal visit')
        if kind == 'state_entered':
            _check(self.active is None and self.budget is None, 'overlapping or premature state visit')
            _check(event.state_visit_id not in self.visit_ids, 'reused state visit ID')
            self.visit_ids.add(event.state_visit_id)
            self.active = (event.state, event.state_visit_id)
            self.invocation = self.outcome = self.closed_terminal = None
            self.transitioned = False
            self.visits[event.state] += 1
        elif kind in ('invocation_started', 'invocation_finished', 'state_exited') or (
                kind == 'transition_selected' and event.outcome is not None):
            _check(self.active == (event.state, event.state_visit_id), 'unmatched state visit')
            if kind == 'invocation_started':
                _check(self.invocation is None, 'multiple invocations in one visit')
                _check(event.invocation_id not in self.invocation_ids, 'reused invocation ID')
                self.invocation_ids.add(event.invocation_id)
                self.invocation = event.invocation_id
                self.invocations[event.state] += 1
            elif kind == 'invocation_finished':
                _check(self.invocation == event.invocation_id and self.outcome is None,
                       'unmatched or repeated invocation outcome')
                self.outcome = event.outcome
                self.outcomes[event.outcome] += 1
                if event.duration_ns is not None:
                    self.invocation_duration += event.duration_ns
                    self.timed_invocations += 1
            elif kind == 'transition_selected':
                _check(self.invocation == event.invocation_id and self.outcome == event.outcome
                       and not self.transitioned, 'unmatched or repeated outcome transition')
                self.transitioned = True
                self.transitions += 1
            else:
                _check(self.invocation is None or self.transitioned, 'state exit before invocation transition')
                if event.duration_ns is not None:
                    self.state_durations[event.state] += event.duration_ns
                    self.timed_visits[event.state] += 1
                self.closed_terminal = event.state if self.invocation is None else None
                self.active = None
                self.invocation = self.outcome = None
        elif kind == 'budget_exceeded':
            _check(self.active is None and self.budget is None, 'budget event during active visit or pending budget')
            self.budget = event.state
            self.closed_terminal = None
            self.budgets += 1
        elif kind == 'transition_selected':
            _check(self.budget == event.state, 'budget transition without budget event')
            self.budget = None
            self.transitions += 1
        elif kind == 'run_finished':
            _check(self.active is None and self.budget is None
                   and self.closed_terminal == event.state, 'completion without closed terminal visit')
            self.terminal = event.state
            self.ended = True
            self.run_duration = event.duration_ns
        elif kind == 'run_error':
            if self.active is not None:
                _check(self.active == (event.state, event.state_visit_id)
                       and self.invocation == event.invocation_id
                       and self.outcome == event.outcome, 'unmatched run error')
            else:
                _check(event.invocation_id is None, 'run error without active invocation')
                _check((event.trigger_event is None and self.budget is None) or
                       (event.trigger_event == 'budget_exceeded' and self.budget == event.state),
                       'unmatched budget error')
            self.ended = True
            self.run_duration = event.duration_ns

    def result(self):
        invocations = sum(self.invocations.values())
        invocation_duration = (self.invocation_duration
                               if self.timed_invocations == invocations
                               and (invocations > 0 or self.timed_start) else None)
        state_durations = {state: self.state_durations[state]
                           if self.timed_visits[state] == count else None
                           for state, count in self.visits.items()}
        return RunSummary(self.run_id, self.terminal, self.visits,
                          sum(self.invocations.values()), self.invocations,
                          self.outcomes, self.transitions, self.budgets,
                          self.terminal is not None, self.run_duration,
                          invocation_duration, state_durations)


def summarize_runs(events: Iterable[Event]) -> tuple[RunSummary, ...]:
    """Summarize runs in first-seen order, accepting structurally valid prefixes.

    Interleaved runs are supported. Checks cover record shape and event pairing,
    not graph correctness, configured budgets, or executor/diagnostic contracts.
    No results are returned if any input record or run is invalid.
    """
    runs = {}
    for number, event in enumerate(events, 1):
        try:
            _validate(event)
            if event.run_id not in runs:
                runs[event.run_id] = _Run(event.run_id)
            runs[event.run_id].consume(event)
        except ObservabilityError as exc:
            raise ObservabilityError(f'event {number}: {exc}') from None
    return tuple(run.result() for run in runs.values())
