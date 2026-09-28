"""Version-2 workflow descriptions only: no compilation or execution.

The existing FLOW-001 parser supplies strict YAML syntax handling and its error
boundary. Schema/domain validation here is independent of runtime Flow validation.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Literal, Mapping

from flow_config import FlowConfigError, _at, _error, _fields, _mapping, _yaml_data

Provider = Literal['claude', 'codex', 'command']
Effort = Literal['low', 'medium', 'high']
EscalationTrigger = Literal['inconclusive', 'budget_exceeded', 'diagnosis_disproved', 'verification_same_failure']
ESCALATION_TRIGGERS = frozenset({'inconclusive', 'budget_exceeded', 'diagnosis_disproved', 'verification_same_failure'})


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    max_invocations: int | None = None
    max_total_invocations: int | None = None
    max_escalations: int | None = None
    max_state_visits: int | None = None
    max_duration_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    provider: Provider
    model: str | None = None


@dataclass(frozen=True, slots=True)
class InvocationBinding:
    profile: str
    effort: Effort | None = None
    budget: BudgetSpec | None = None


@dataclass(frozen=True, slots=True)
class EscalationStep:
    kind: Literal['vertical', 'horizontal']
    on: tuple[EscalationTrigger, ...]
    binding: InvocationBinding
    context: Literal['continue', 'fresh']

    def __post_init__(self):
        object.__setattr__(self, 'on', tuple(self.on))


@dataclass(frozen=True, slots=True)
class RoleBinding:
    primary: InvocationBinding
    escalation: tuple[EscalationStep, ...] = ()
    budget: BudgetSpec | None = None

    def __post_init__(self):
        object.__setattr__(self, 'escalation', tuple(self.escalation))


@dataclass(frozen=True, slots=True)
class WorkflowStateSpec:
    role: str | None = None
    on: Mapping[str, str] = field(default_factory=dict)
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    terminal: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'on', MappingProxyType(dict(self.on)))
        object.__setattr__(self, 'consumes', tuple(self.consumes))
        object.__setattr__(self, 'produces', tuple(self.produces))


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    entry: str
    run_budget: BudgetSpec
    states: Mapping[str, WorkflowStateSpec]

    def __post_init__(self):
        object.__setattr__(self, 'states', MappingProxyType(dict(self.states)))


@dataclass(frozen=True, slots=True)
class WorkflowBundle:
    version: int
    profiles: Mapping[str, ExecutionProfile]
    roles: Mapping[str, RoleBinding]
    flows: Mapping[str, WorkflowSpec]

    def __post_init__(self):
        for name in ('profiles', 'roles', 'flows'):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def flow(self, name: str) -> WorkflowSpec:
        _name(name, 'flows')
        if name not in self.flows:
            raise _error(_at('flows', name), f'unknown flow {name!r}')
        return self.flows[name]


def _name(value, path):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', value):
        raise _error(path, 'expected logical identifier [A-Za-z][A-Za-z0-9_.-]*')
    return value


def _enum(value, choices, path):
    if type(value) is not str or value not in choices:
        raise _error(path, 'expected one of: ' + ', '.join(sorted(choices)))
    return value


def _nonempty_mapping(value, path):
    _mapping(value, path)
    if not value:
        raise _error(path, 'expected nonempty mapping')


def _names(value, path, *, triggers=False):
    if type(value) is not list:
        raise _error(path, 'expected list')
    seen = set()
    for i, item in enumerate(value):
        location = f'{path}[{i}]'
        _enum(item, ESCALATION_TRIGGERS, location) if triggers else _name(item, location)
        if item in seen:
            raise _error(location, 'duplicate value')
        seen.add(item)
    if triggers and not value:
        raise _error(path, 'expected nonempty trigger list')
    return tuple(value)


def _budget(value, scope, path):
    allowed = {
        'rung': {'max_invocations', 'max_duration_seconds'},
        'role': {'max_total_invocations', 'max_escalations', 'max_duration_seconds'},
        'run': {'max_invocations', 'max_state_visits', 'max_duration_seconds'},
    }[scope]
    _fields(value, {'max_invocations'} if scope == 'run' else set(), allowed, path)
    _nonempty_mapping(value, path)
    for key, limit in value.items():
        if type(limit) is not int or limit <= 0:
            raise _error(_at(path, key), 'expected positive integer, excluding bool')
    return BudgetSpec(**value)


def _binding(data, profiles, path):
    # Field whitelisting is done by the primary/step callers (step YAML is flat).
    ref = _name(data['profile'], path + '.profile')
    if ref not in profiles:
        raise _error(path + '.profile', f'unknown profile {ref!r}')
    effort = None
    if 'effort' in data:
        if profiles[ref].provider == 'command':
            raise _error(path + '.effort', 'command bindings must omit effort')
        effort = _enum(data['effort'], {'low', 'medium', 'high'}, path + '.effort')
    budget = _budget(data['budget'], 'rung', path + '.budget') if 'budget' in data else None
    return InvocationBinding(ref, effort, budget)


def _role(data, profiles, path):
    _fields(data, {'primary'}, {'escalation', 'budget'}, path)
    _fields(data['primary'], {'profile'}, {'effort', 'budget'}, path + '.primary')
    primary = previous = _binding(data['primary'], profiles, path + '.primary')
    ladder = data.get('escalation', [])
    if type(ladder) is not list:
        raise _error(path + '.escalation', 'expected list')
    steps = []
    for i, step in enumerate(ladder):
        here = f'{path}.escalation[{i}]'
        _fields(step, {'kind', 'on', 'profile', 'context'}, {'effort', 'budget'}, here)
        kind = _enum(step['kind'], {'vertical', 'horizontal'}, here + '.kind')
        triggers = _names(step['on'], here + '.on', triggers=True)
        context = _enum(step['context'], {'continue', 'fresh'}, here + '.context')
        binding = _binding(step, profiles, here)
        same_provider = profiles[previous.profile].provider == profiles[binding.profile].provider
        if kind == 'vertical':
            if not same_provider:
                raise _error(here + '.profile', 'vertical escalation requires the previous provider')
            if context != 'continue':
                raise _error(here + '.context', 'vertical escalation requires continue')
            if (previous.profile, previous.effort) == (binding.profile, binding.effort):
                raise _error(here, 'vertical escalation must change profile and/or effort')
        else:
            if same_provider:
                raise _error(here + '.profile', 'horizontal escalation requires a different provider')
            if context != 'fresh':
                raise _error(here + '.context', 'horizontal escalation requires fresh')
        steps.append(EscalationStep(kind, triggers, binding, context))
        previous = binding
    budget = _budget(data['budget'], 'role', path + '.budget') if 'budget' in data else None
    if budget is not None and budget.max_escalations is not None:
        if budget.max_escalations > len(steps):
            raise _error(path + '.budget.max_escalations', 'exceeds configured ladder length')
        if budget.max_total_invocations is not None and budget.max_escalations >= budget.max_total_invocations:
            raise _error(path + '.budget.max_escalations', 'requires primary plus escalation invocations within role total')
    return RoleBinding(primary, tuple(steps), budget)


def _workflow(data, roles, path):
    _fields(data, {'entry', 'run_budget', 'states'}, set(), path)
    entry = _name(data['entry'], path + '.entry')
    budget = _budget(data['run_budget'], 'run', path + '.run_budget')
    _nonempty_mapping(data['states'], path + '.states')
    states = {}
    for name, state in data['states'].items():
        here = _at(path + '.states', name)
        _name(name, here)
        _mapping(state, here)
        if 'terminal' in state:
            if state['terminal'] is not True:
                raise _error(here + '.terminal', 'only terminal: true is permitted; normal states omit terminal')
            _fields(state, {'terminal'}, set(), here)
            states[name] = WorkflowStateSpec(terminal=True)
            continue
        _fields(state, {'role', 'on'}, {'consumes', 'produces'}, here)
        role = _name(state['role'], here + '.role')
        if role not in roles:
            raise _error(here + '.role', f'unknown role {role!r}')
        consumes = _names(state.get('consumes', []), here + '.consumes')
        produces = _names(state.get('produces', []), here + '.produces')
        _nonempty_mapping(state['on'], here + '.on')
        for outcome, target in state['on'].items():
            location = _at(here + '.on', outcome)
            _name(outcome, location)
            _name(target, location)
            if target not in data['states']:
                raise _error(location, f'unknown target {target!r}')
        states[name] = WorkflowStateSpec(role, state['on'], consumes, produces)
    if entry not in states:
        raise _error(path + '.entry', f'unknown entry {entry!r}')
    seen, pending = set(), [entry]
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(states[name].on.values())
    for name in states:
        if name not in seen:
            raise _error(_at(path + '.states', name), 'unreachable state')
    return WorkflowSpec(entry, budget, states)


def load_workflow_bundle(path: str | os.PathLike[str]) -> WorkflowBundle:
    """Read/validate UTF-8 v2 YAML. Never execute, compile or resolve machine paths."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise _error('$', 'cannot read UTF-8 workflow bundle') from exc
    data = _yaml_data(text)
    _fields(data, {'version', 'profiles', 'roles', 'flows'}, set(), '')
    if type(data['version']) is not int or data['version'] != 2:
        raise _error('version', 'expected workflow bundle version 2 (integer, excluding bool)')
    for collection in ('profiles', 'roles', 'flows'):
        _nonempty_mapping(data[collection], collection)
        for name in data[collection]:
            _name(name, _at(collection, name))
    profiles = {}
    for name, definition in data['profiles'].items():
        here = _at('profiles', name)
        _fields(definition, {'provider'}, {'model'}, here)
        provider = _enum(definition['provider'], {'claude', 'codex', 'command'}, here + '.provider')
        if provider == 'command':
            if 'model' in definition:
                raise _error(here + '.model', 'command profile must omit model')
            model = None
        else:
            _fields(definition, {'provider', 'model'}, set(), here)
            model = _name(definition['model'], here + '.model')
        profiles[name] = ExecutionProfile(provider, model)
    roles = {name: _role(definition, profiles, _at('roles', name)) for name, definition in data['roles'].items()}
    flows = {name: _workflow(definition, roles, _at('flows', name)) for name, definition in data['flows'].items()}
    return WorkflowBundle(2, profiles, roles, flows)
