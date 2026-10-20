"""Version-2 workflow descriptions only: no compilation or execution.

The existing FLOW-001 parser supplies strict YAML syntax handling and its error
boundary. Schema/domain validation here is independent of runtime Flow validation.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import math
from types import MappingProxyType
from typing import Literal, Mapping

from flow_config import FlowConfigError, _at, _error, _fields, _mapping, _yaml_data

CAPABILITIES = ('repo_read', 'repo_write', 'command')

Provider = Literal['claude', 'codex', 'command']
Effort = Literal['low', 'medium', 'high']
EscalationTrigger = Literal['inconclusive', 'budget_exceeded', 'diagnosis_disproved', 'verification_same_failure']
ESCALATION_TRIGGERS = frozenset({'inconclusive', 'budget_exceeded', 'diagnosis_disproved', 'verification_same_failure'})


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    format: Literal['markdown', 'text', 'yaml', 'json']
    schema: Literal['microspec'] | None = None
    source: Literal['request'] | None = None


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    max_invocations: int | None = None
    max_state_visits: int | None = None
    on_exceeded: str | None = None


@dataclass(frozen=True, slots=True)
class CommandOutcomes:
    exit_codes: Mapping[int, str]
    default: str

    def __post_init__(self):
        object.__setattr__(self, 'exit_codes', MappingProxyType(dict(self.exit_codes)))


@dataclass(frozen=True, slots=True)
class CommandCapture:
    artifact: str
    stream: Literal['stdout', 'stderr', 'combined']
    max_bytes: int


@dataclass(frozen=True, slots=True)
class CommandSpec:
    argv: tuple[str, ...]
    outcomes: CommandOutcomes
    capture: CommandCapture | None = None

    def __post_init__(self):
        object.__setattr__(self, 'argv', tuple(self.argv))


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    provider: Provider
    model: str | None = None
    command: CommandSpec | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class InvocationBinding:
    profile: str
    effort: Effort | None = None
    budget: BudgetSpec = field(default_factory=lambda: BudgetSpec(max_invocations=1))
    outcomes: tuple[str, ...] | None = None

    def __post_init__(self):
        if self.outcomes is not None:
            object.__setattr__(self, 'outcomes', tuple(self.outcomes))


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

    def __post_init__(self):
        object.__setattr__(self, 'escalation', tuple(self.escalation))


@dataclass(frozen=True, slots=True)
class WorkflowStateSpec:
    role: str | None = None
    on: Mapping[str, str] = field(default_factory=dict)
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    terminal: bool = False
    budget: BudgetSpec | None = None
    escalation_exhausted: str | None = None
    capabilities: tuple[str, ...] | None = None
    resume_to: str | None = None
    wait_for: str | None = None
    rung_budget_scope: str = "run"

    @property
    def kind(self) -> str:
        from runtime import state_kind
        return state_kind(self.terminal, self.wait_for, self.resume_to)

    def __post_init__(self):
        object.__setattr__(self, 'on', MappingProxyType(dict(self.on)))
        object.__setattr__(self, 'consumes', tuple(self.consumes))
        object.__setattr__(self, 'produces', tuple(self.produces))
        if self.capabilities is not None:
            if any(c not in CAPABILITIES for c in self.capabilities):
                raise ValueError('unknown stage capability')
            object.__setattr__(self, 'capabilities', tuple(c for c in CAPABILITIES if c in self.capabilities))


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
    artifacts: Mapping[str, ArtifactSpec] = field(default_factory=dict)

    def __post_init__(self):
        for name in ('profiles', 'roles', 'flows', 'artifacts'):
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
        'rung': {'max_invocations'},
        'state': {'max_invocations', 'max_state_visits', 'on_exceeded'},
        'run': {'max_invocations', 'on_exceeded'},
    }[scope]
    required = {'max_invocations'} if scope == 'rung' else {'on_exceeded'}
    if scope == 'run':
        required.add('max_invocations')
    _fields(value, required, allowed, path)
    _nonempty_mapping(value, path)
    if not any(key in value for key in ('max_invocations', 'max_state_visits')):
        raise _error(path, 'expected at least one numeric limit')
    for key, limit in value.items():
        if key == 'on_exceeded':
            _name(limit, _at(path, key))
            continue
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
    budget = _budget(data['budget'], 'rung', path + '.budget') if 'budget' in data else BudgetSpec(max_invocations=1)
    outcomes = None
    if 'outcomes' in data:
        outcomes = _names(data['outcomes'], path + '.outcomes')
        if not outcomes:
            raise _error(path + '.outcomes', 'expected nonempty outcome list')
        if 'budget_exceeded' in outcomes:
            raise _error(path + '.outcomes', 'reserved control-plane outcome')
        command = profiles[ref].command
        if command is not None:
            produced = set(command.outcomes.exit_codes.values()) | {command.outcomes.default}
            if not produced.issubset(outcomes):
                raise _error(path + '.outcomes', 'restriction excludes command exit outcomes')
    return InvocationBinding(ref, effort, budget, outcomes)


def _role(data, profiles, path):
    _fields(data, {'primary'}, {'escalation'}, path)
    _fields(data['primary'], {'profile'}, {'effort', 'budget', 'outcomes'}, path + '.primary')
    primary = previous = _binding(data['primary'], profiles, path + '.primary')
    ladder = data.get('escalation', [])
    if type(ladder) is not list:
        raise _error(path + '.escalation', 'expected list')
    steps = []
    for i, step in enumerate(ladder):
        here = f'{path}.escalation[{i}]'
        _fields(step, {'kind', 'on', 'profile', 'context'}, {'effort', 'budget', 'outcomes'}, here)
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
    return RoleBinding(primary, tuple(steps))


def _artifact_catalog(data, path='artifacts'):
    _mapping(data, path)
    result = {}
    for name, definition in data.items():
        here = _at(path, name) if type(name) is str else path
        _name(name, here)
        _fields(definition, {'format'}, {'schema', 'source'}, here)
        fmt = _enum(definition['format'], {'markdown', 'text', 'yaml', 'json'}, here + '.format')
        schema = _enum(definition['schema'], {'microspec'}, here + '.schema') if 'schema' in definition else None
        source = _enum(definition['source'], {'request'}, here + '.source') if 'source' in definition else None
        if schema is not None and fmt not in ('yaml', 'json'):
            raise _error(here + '.schema', 'microspec requires yaml or json')
        if source == 'request' and (name != 'request' or fmt not in ('markdown', 'text') or schema is not None):
            raise _error(here + '.source', 'request source requires name request and markdown/text without schema')
        if name == 'request' and source != 'request':
            raise _error(here + '.source', 'request must be the immutable request source')
        result[name] = ArtifactSpec(fmt, schema, source)
    if result and ('request' not in result or result['request'].source != 'request'):
        raise _error(path, 'nonempty catalog requires the immutable request artifact')
    return result


def _command(data, artifacts, path):
    _fields(data, {'argv', 'outcomes'}, {'capture'}, path)
    argv = data['argv']
    if type(argv) is not list or not argv:
        raise _error(path + '.argv', 'expected nonempty list')
    for index, arg in enumerate(argv):
        if type(arg) is not str or not arg or '\0' in arg:
            raise _error(f'{path}.argv[{index}]', 'expected nonempty string without NUL')
    outcomes = data['outcomes']
    _fields(outcomes, {'exit_codes', 'default'}, set(), path + '.outcomes')
    _mapping(outcomes['exit_codes'], path + '.outcomes.exit_codes')
    def outcome(value, where):
        _name(value, where)
        if value == 'budget_exceeded':
            raise _error(where, 'reserved control-plane outcome')
        return value
    codes = {}
    for code, value in outcomes['exit_codes'].items():
        where = path + '.outcomes.exit_codes'
        if type(code) is not int or code < 0:
            raise _error(where, 'expected nonnegative integer exit code, excluding bool')
        label = str(code) if code.bit_length() < 256 else '<large integer>'
        codes[code] = outcome(value, f'{where}[{label}]')
    default = outcome(outcomes['default'], path + '.outcomes.default')
    capture = None
    if 'capture' in data:
        raw = data['capture']; here = path + '.capture'
        _fields(raw, {'artifact', 'stream', 'max_bytes'}, set(), here)
        name = _name(raw['artifact'], here + '.artifact')
        if name not in artifacts:
            raise _error(here + '.artifact', 'undeclared artifact')
        spec = artifacts[name]
        if spec.source == 'request' or spec.format not in ('text', 'markdown') or spec.schema is not None:
            raise _error(here + '.artifact', 'capture requires non-request text/markdown without schema')
        stream = _enum(raw['stream'], {'stdout','stderr','combined'}, here + '.stream')
        limit = raw['max_bytes']
        if type(limit) is not int or limit <= 0:
            raise _error(here + '.max_bytes', 'expected positive integer, excluding bool')
        capture = CommandCapture(name, stream, limit)
    return CommandSpec(tuple(argv), CommandOutcomes(codes, default), capture)


def _command_uses(flow, roles, profiles, path):
    for name, state in flow.states.items():
        if state.terminal or state.wait_for:
            continue
        role = roles[state.role]
        bindings = (role.primary,) + tuple(step.binding for step in role.escalation)
        for index, binding in enumerate(bindings):
            command = profiles[binding.profile].command
            if command is None:
                continue
            here = _at(path + '.states', name)
            if state.capabilities is not None:
                raise _error(here + '.capabilities', 'model capabilities forbidden on command-provider states')
            next_triggers = role.escalation[index].on if index < len(role.escalation) else ()
            for value in (*command.outcomes.exit_codes.values(), command.outcomes.default):
                if (value not in next_triggers and value not in state.on and
                        not (value in ESCALATION_TRIGGERS and state.escalation_exhausted is not None)):
                    raise _error(here + '.on', f'command profile {binding.profile!r} produces unhandled outcome {value!r}')
            if command.capture and command.capture.artifact not in state.produces:
                raise _error(here + '.produces', f'missing command capture artifact {command.capture.artifact!r}')


def _workflow(data, roles, artifacts, path):
    _fields(data, {'entry', 'run_budget', 'states'}, set(), path)
    entry = _name(data['entry'], path + '.entry')
    budget = _budget(data['run_budget'], 'run', path + '.run_budget')
    _nonempty_mapping(data['states'], path + '.states')
    states = {}
    for name, state in data['states'].items():
        here = _at(path + '.states', name)
        _name(name, here)
        _mapping(state, here)
        if 'wait_for' in state:
            _fields(state, {'wait_for', 'resume_to', 'budget'}, set(), here)
            _enum(state['wait_for'], {'human_input'}, here + '.wait_for')
            target = _name(state['resume_to'], here + '.resume_to')
            declaration = data['states'].get(target)
            if type(declaration) is not dict or 'terminal' in declaration or 'wait_for' in declaration:
                raise _error(here + '.resume_to', 'expected an executable state of this flow')
            bound = _budget(state['budget'], 'state', here + '.budget')
            if bound.max_state_visits is None or bound.max_invocations is not None:
                raise _error(here + '.budget', 'human wait requires only max_state_visits and on_exceeded')
            states[name] = WorkflowStateSpec(wait_for='human_input', resume_to=target, budget=bound)
            continue
        if 'terminal' in state:
            if state['terminal'] is not True:
                raise _error(here + '.terminal', 'only terminal: true is permitted; normal states omit terminal')
            _fields(state, {'terminal'}, {'resume_to'}, here)
            resume_to = None
            if 'resume_to' in state:
                resume_to = _name(state['resume_to'], here + '.resume_to')
                target = data['states'].get(resume_to)
                if type(target) is not dict or 'terminal' in target or 'wait_for' in target:
                    raise _error(here + '.resume_to', f'expected a nonterminal state of this flow, got {resume_to!r}')
            states[name] = WorkflowStateSpec(terminal=True, resume_to=resume_to)
            continue
        _fields(state, {'role', 'on'}, {'consumes', 'produces', 'budget', 'escalation_exhausted', 'capabilities', 'rung_budget_scope'}, here)
        role = _name(state['role'], here + '.role')
        if role not in roles:
            raise _error(here + '.role', f'unknown role {role!r}')
        capabilities = None
        if 'capabilities' in state:
            capabilities = _names(state['capabilities'], here + '.capabilities')
            for capability in capabilities:
                _enum(capability, CAPABILITIES, here + '.capabilities')
            if capabilities and 'repo_read' not in capabilities:
                raise _error(here + '.capabilities', 'repo_write and command require repo_read')
        local_budget = _budget(state['budget'], 'state', here + '.budget') if 'budget' in state else None
        exhaustion = None
        if roles[role].escalation:
            if 'escalation_exhausted' not in state:
                raise _error(here + '.escalation_exhausted', 'required for a role with an escalation ladder')
            exhaustion = _name(state['escalation_exhausted'], here + '.escalation_exhausted')
            if exhaustion not in data['states']:
                raise _error(here + '.escalation_exhausted', f'unknown target {exhaustion!r}')
        elif 'escalation_exhausted' in state:
            raise _error(here + '.escalation_exhausted', 'only allowed for a role with an escalation ladder')
        consumes = _names(state.get('consumes', []), here + '.consumes')
        produces = _names(state.get('produces', []), here + '.produces')
        for field_name, references in (('consumes', consumes), ('produces', produces)):
            for reference in references:
                if reference not in artifacts:
                    raise _error(here + '.' + field_name, f'undeclared artifact {reference!r}')
                if field_name == 'produces' and artifacts[reference].source == 'request':
                    raise _error(here + '.produces', 'cannot produce immutable request')
        _nonempty_mapping(state['on'], here + '.on')
        for outcome, target in state['on'].items():
            location = _at(here + '.on', outcome)
            _name(outcome, location)
            _name(target, location)
            if target not in data['states']:
                raise _error(location, f'unknown target {target!r}')
        states[name] = WorkflowStateSpec(role, state['on'], consumes, produces, budget=local_budget, escalation_exhausted=exhaustion, capabilities=capabilities,
            rung_budget_scope=_enum(state.get('rung_budget_scope', 'run'), {'run', 'visit'}, here + '.rung_budget_scope'))
    run_target = budget.on_exceeded
    if run_target not in states:
        raise _error(path + '.run_budget.on_exceeded', f'unknown target {run_target!r}')
    if not states[run_target].terminal:
        raise _error(path + '.run_budget.on_exceeded', 'target must be terminal')
    for name, state in states.items():
        if state.budget is not None and state.budget.on_exceeded not in states:
            raise _error(_at(path + '.states', name) + '.budget.on_exceeded',
                         f'unknown target {state.budget.on_exceeded!r}')
    for name, state in states.items():
        control_targets = [state.on.get('budget_exceeded'), state.escalation_exhausted]
        if state.budget:
            control_targets.append(state.budget.on_exceeded)
        if any(target is not None and states[target].wait_for for target in control_targets):
            raise _error(path + '.states.' + name, 'human wait requires a semantic outcome, not a budget/exhaustion route')
    if entry not in states:
        raise _error(path + '.entry', f'unknown entry {entry!r}')
    if states[entry].wait_for:
        raise _error(path + '.entry', 'human wait requires an originating provider result')
    for name, state in states.items():
        if state.wait_for and not states[state.budget.on_exceeded].terminal:
            raise _error(path + '.states.' + name + '.budget', 'human wait exhaustion must target a terminal')
    seen, pending = set(), [entry, run_target]
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(states[name].on.values())
            if states[name].resume_to is not None:
                pending.append(states[name].resume_to)
            if states[name].budget is not None:
                pending.append(states[name].budget.on_exceeded)
            if states[name].escalation_exhausted is not None:
                pending.append(states[name].escalation_exhausted)
    for name in states:
        if name not in seen:
            raise _error(_at(path + '.states', name), 'unreachable state')
    return WorkflowSpec(entry, budget, states)


def _bundle_yaml_data(text):
    return _yaml_data(text, integer_keys_at=lambda path: re.fullmatch(
        r'profiles(?:\.[A-Za-z_][A-Za-z0-9_-]*|\["[^"\\]+"\])\.command\.outcomes\.exit_codes', path) is not None)


def load_workflow_bundle(path: str | os.PathLike[str]) -> WorkflowBundle:
    """Read/validate UTF-8 v2 YAML. Never execute, compile or resolve machine paths."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise _error('$', 'cannot read UTF-8 workflow bundle') from exc
    return workflow_bundle_from_data(_bundle_yaml_data(text))


def workflow_bundle_from_data(data) -> WorkflowBundle:
    """Validate a normalized V2 mapping without filesystem or provider operations."""
    _fields(data, {'version', 'profiles', 'roles', 'flows'}, {'artifacts'}, '')
    if type(data['version']) is not int or data['version'] != 2:
        raise _error('version', 'expected workflow bundle version 2 (integer, excluding bool)')
    for collection in ('profiles', 'roles', 'flows'):
        _nonempty_mapping(data[collection], collection)
        for name in data[collection]:
            _name(name, _at(collection, name))
    artifacts = _artifact_catalog(data.get('artifacts', {}))
    profiles = {}
    for name, definition in data['profiles'].items():
        here = _at('profiles', name)
        _fields(definition, {'provider'}, {'model', 'command', 'timeout_seconds'}, here)
        provider = _enum(definition['provider'], {'claude', 'codex', 'command'}, here + '.provider')
        if provider == 'command':
            if 'model' in definition:
                raise _error(here + '.model', 'command profile must omit model')
            _fields(definition, {'provider', 'command'}, {'timeout_seconds'}, here)
            model = None
        else:
            _fields(definition, {'provider', 'model'}, {'timeout_seconds'}, here)
            model = _name(definition['model'], here + '.model')
        command = _command(definition['command'], artifacts, here + '.command') if provider == 'command' else None
        timeout = definition.get('timeout_seconds')
        if 'timeout_seconds' in definition and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise _error(here+'.timeout_seconds', 'expected finite positive seconds')
        profiles[name] = ExecutionProfile(provider, model, command, timeout)
    roles = {name: _role(definition, profiles, _at('roles', name)) for name, definition in data['roles'].items()}
    flows = {name: _workflow(definition, roles, artifacts, _at('flows', name)) for name, definition in data['flows'].items()}
    for name, flow in flows.items():
        _command_uses(flow, roles, profiles, _at('flows', name))
    return WorkflowBundle(2, profiles, roles, flows, artifacts)
