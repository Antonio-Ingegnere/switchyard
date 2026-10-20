"""One project file, lowered deterministically to the existing V2 engine."""
from dataclasses import dataclass
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from flow_config import FlowConfigError, _error, _fields, _mapping, _yaml_data, _at
from .workflow_bundle import (WorkflowBundle, workflow_bundle_from_data, _name, _names,
                              _enum, _nonempty_mapping)
from .workflow_compiler import ExecutionBinding, compile_workflow, WorkflowCompileError
from .provider_stages import validate_provider_configuration, ProviderResolutionError


@dataclass(frozen=True)
class AgentConfig:
    provider: str
    model: str | None
    effort: str | None
    workspace: str | None
    shell: bool | None
    capabilities: tuple[str, ...] | None
    timeout: float = 600
    infrastructure_failures: int = 2  # Provider failures without a StageResult before resume refuses.


@dataclass(frozen=True)
class LifecyclePolicy:
    manual_qa: bool = True
    commit: bool = True
    reopen: bool = True


@dataclass(frozen=True)
class SimpleConfig:
    bundle: WorkflowBundle
    agents: Mapping[str, AgentConfig]
    lifecycle: LifecyclePolicy

    def __post_init__(self):
        object.__setattr__(self, 'agents', MappingProxyType(dict(self.agents)))

    def show(self):
        """Effective values; no executable lookup, environment, aliases or provider calls."""
        rows = []
        for flow in self.bundle.flows:
            compiled = compile_workflow(self.bundle, flow, lambda binding: _NeverExecute())
            for plan in compiled.plan.values():
                b = plan.binding
                if b is None:
                    continue
                agent = self.agents[b.profile_name]
                command = self.bundle.profiles[b.profile_name].command
                rows.append(dict(flow=flow, state=b.logical_state, rung=b.rung_index,
                    agent=b.profile_name, provider=b.provider, model=b.model, effort=b.effort,
                    workspace=agent.workspace, shell=agent.shell, timeout=agent.timeout,
                    infrastructure_failures=agent.infrastructure_failures, context=b.context_mode,
                    escalation=b.escalation_kind, outcomes=list(b.executor_allowed_outcomes),
                    consumes=list(plan.declaration.consumes), produces=list(plan.declaration.produces),
                    command=list(command.argv) if command else None))
        from dataclasses import asdict
        flows = {name: dict(start=flow.entry,
                 limits=dict(run_invocations=flow.run_budget.max_invocations, on_exceeded=flow.run_budget.on_exceeded),
                 states={key: dict(state_kind=state.kind, terminal=state.terminal, on=dict(state.on),
                     budget=None if state.budget is None else dict(invocations=state.budget.max_invocations,
                         visits=state.budget.max_state_visits, on_exceeded=state.budget.on_exceeded),
                     on_exhausted=state.escalation_exhausted, resume_to=state.resume_to,
                     wait_for=state.wait_for, rung_budget_scope=state.rung_budget_scope) for key,state in flow.states.items()})
                 for name,flow in self.bundle.flows.items()}
        return dict(invocations=rows, flows=flows, lifecycle=asdict(self.lifecycle))


class _NeverExecute:
    def run(self, request):
        raise AssertionError('configuration validation does not execute')


def _boolean(value, path):
    if type(value) is not bool:
        raise _error(path, 'expected boolean true or false')
    return value


def _positive(value, path):
    if type(value) is not int or value <= 0:
        raise _error(path, 'expected positive integer')
    return value


def _lifecycle(value):
    _fields(value, set(), {'manual_qa', 'accept', 'reopen'}, 'lifecycle')
    qa = _boolean(value.get('manual_qa', True), 'lifecycle.manual_qa')
    accept = value.get('accept', {})
    _fields(accept, set(), {'commit'}, 'lifecycle.accept')
    commit = _boolean(accept.get('commit', qa), 'lifecycle.accept.commit')
    reopen = _boolean(value.get('reopen', qa), 'lifecycle.reopen')
    if not qa and (commit or reopen):
        raise _error('lifecycle', 'accept/reopen require manual_qa=true')
    return LifecyclePolicy(qa, commit, reopen)


def load_simple_config(path='.switchyard/config.yaml') -> SimpleConfig:
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError, TypeError, ValueError):
        raise _error('$', 'cannot read UTF-8 project config') from None
    return simple_config_from_text(text)


def compile_simple_config(config: SimpleConfig) -> WorkflowBundle:
    """Return the already validated V2 bundle for compile_workflow/execute_v2_workflow."""
    return config.bundle


def simple_config_from_text(text: str) -> SimpleConfig:
    return _simple_config_from_data(_yaml_data(text))


def _simple_config_from_data(data):
    _fields(data, {'agents', 'flows'}, {'lifecycle', 'version'}, '')
    if type(data.get('version', 2)) is not int or data.get('version', 2) != 2:
        raise _error('version', 'new project configurations require version 2; declare every target state explicitly')
    lifecycle = _lifecycle(data.get('lifecycle', {}))
    profiles, agents, roles, flows = {}, {}, {}, {}
    artifacts = {'request': {'format': 'markdown', 'source': 'request'}}
    locations = {}
    for collection in ('agents', 'flows'):
        _nonempty_mapping(data[collection], collection)
        for name in data[collection]:
            _name(name, _at(collection, name))
    for name, value in data['agents'].items():
        here = _at('agents', name)
        _fields(value, {'provider'}, {'model','effort','workspace','shell','command','capture','success','failure','timeout','infrastructure_failures'}, here)
        provider = _enum(value['provider'], {'claude','codex','command'}, here+'.provider')
        if provider == 'command':
            _fields(value, {'provider','command'}, {'capture','success','failure','timeout','infrastructure_failures'}, here)
            command = dict(argv=value['command'], outcomes=dict(exit_codes={0:value.get('success','pass')}, default=value.get('failure','fail')))
            if 'capture' in value:
                _name(value['capture'], here+'.capture')
                command['capture'] = dict(artifact=value['capture'], stream='combined', max_bytes=32768)
            profiles[name] = dict(provider=provider, command=command)
            agent = AgentConfig(provider, None, None, None, None, None)
        else:
            _fields(value, {'provider','model','workspace','shell'}, {'effort','timeout','infrastructure_failures'}, here)
            model = _name(value['model'], here+'.model')
            effort = _enum(value['effort'], {'low','medium','high'}, here+'.effort') if 'effort' in value else None
            workspace = _enum(value['workspace'], {'none','read','write'}, here+'.workspace')
            shell = _boolean(value['shell'], here+'.shell')
            caps = (() if workspace == 'none' else ('repo_read',)) + (('repo_write',) if workspace == 'write' else ()) + (('command',) if shell else ())
            agent = AgentConfig(provider, model, effort, workspace, shell, caps)
            binding = ExecutionBinding('config', name, name, 0, name, provider, model, effort, None, None, capabilities=caps)
            try:
                validate_provider_configuration(binding)
            except ProviderResolutionError as exc:
                if 'cannot represent capabilities' in str(exc):
                    raise _error(here, f'provider {provider} cannot represent workspace={workspace}, shell={str(shell).lower()}') from None
                raise _error(here, str(exc)) from None
            profiles[name] = dict(provider=provider, model=model)
        timeout = value.get('timeout',600)
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout <= 0:
            raise _error(here+'.timeout','expected finite positive seconds')
        from dataclasses import replace
        agents[name] = replace(agent,timeout=timeout,infrastructure_failures=_positive(
            value.get('infrastructure_failures',2),here+'.infrastructure_failures'))
        profiles[name]['timeout_seconds'] = timeout
        locations[_at('profiles', name)] = here
    for flow_name, flow in data['flows'].items():
        here = _at('flows', flow_name)
        _fields(flow, {'start','states'}, {'limits'}, here)
        _nonempty_mapping(flow['states'], here+'.states')
        limits = flow.get('limits', {})
        _fields(limits, set(), {'run_invocations','on_exceeded'}, here+'.limits')
        states = {}
        for state_name, state in flow['states'].items():
            path = _at(here+'.states', state_name)
            _name(state_name, path)
            _mapping(state, path)
            if 'wait_for' in state:
                _fields(state, {'wait_for', 'resume_to', 'budget'}, set(), path)
                bound = state['budget']
                _fields(bound, {'visits', 'on_exceeded'}, set(), path+'.budget')
                states[state_name] = dict(wait_for=state['wait_for'], resume_to=state['resume_to'],
                    budget=dict(max_state_visits=_positive(bound['visits'], path+'.budget.visits'),
                                on_exceeded=bound['on_exceeded']))
                continue
            if 'terminal' in state:
                _fields(state, {'terminal'}, {'resume_to'}, path)
                if state['terminal'] is not True:
                    raise _error(path+'.terminal', 'expected true')
                states[state_name] = {'terminal': True}
                if 'resume_to' in state:
                    states[state_name]['resume_to'] = state['resume_to']
                continue
            _fields(state, {'on'}, {'agent','run','consumes','produces','budget','on_exhausted','rung_budget_scope'}, path)
            if ('agent' in state) == ('run' in state):
                raise _error(path, 'declare exactly one of agent or run')
            ladder = state.get('run', [{'agent':state.get('agent')}])
            if type(ladder) is not list or not ladder:
                raise _error(path+'.run', 'expected nonempty sequence')
            role_name = f'state-{len(roles)}'
            rungs, permissions = [], []
            for index, rung in enumerate(ladder):
                at = f'{path}.run[{index}]' if 'run' in state else path
                required = {'agent'} if index == 0 else {'agent','when','context'}
                _fields(rung, required, {'outcomes','budget'}, at)
                reference = _name(rung['agent'], at+'.agent')
                if reference not in agents:
                    raise _error(at+'.agent', f'unknown agent {reference!r}')
                agent = agents[reference]
                permissions.append(agent.capabilities)
                normalized = dict(profile=reference)
                if agent.effort is not None:
                    normalized['effort'] = agent.effort
                if 'outcomes' in rung:
                    normalized['outcomes'] = rung['outcomes']
                budget = rung.get('budget', {})
                _fields(budget, set(), {'invocations'}, at+'.budget')
                normalized['budget'] = {'max_invocations':_positive(budget.get('invocations',1),at+'.budget.invocations')}
                if index:
                    previous = agents[ladder[index-1]['agent']]
                    normalized.update(kind='vertical' if previous.provider == agent.provider else 'horizontal',
                                      on=rung['when'], context=rung['context'])
                locations[f'roles.{role_name}.'+('primary' if not index else f'escalation[{index-1}]')] = at
                rungs.append(normalized)
            if any(p != permissions[0] for p in permissions):
                raise _error(path+'.run', 'all agents in one state must have identical workspace/shell permissions')
            roles[role_name] = dict(primary=rungs[0], escalation=rungs[1:])
            budget = state.get('budget', {})
            _fields(budget, set(), {'invocations','visits','on_exceeded'}, path+'.budget')
            normalized = dict(role=role_name, on=state['on'], consumes=state.get('consumes', []), produces=[],
                budget=dict(max_invocations=_positive(budget.get('invocations',sum(r['budget']['max_invocations'] for r in rungs)),path+'.budget.invocations'),
                            max_state_visits=_positive(budget.get('visits',1),path+'.budget.visits'),
                            on_exceeded=budget.get('on_exceeded','NEEDS_HUMAN')))
            normalized['rung_budget_scope'] = _enum(state.get('rung_budget_scope', 'run'), {'run', 'visit'}, path+'.rung_budget_scope')
            if normalized['rung_budget_scope'] == 'visit' and 'invocations' not in budget:
                del normalized['budget']['max_invocations']  # No implicit cumulative cap for explicitly per-visit rungs.
            if permissions[0] is not None:
                normalized['capabilities'] = list(permissions[0])
            if len(rungs)>1:
                normalized['escalation_exhausted'] = state.get('on_exhausted','NEEDS_HUMAN')
            elif 'on_exhausted' in state:
                raise _error(path+'.on_exhausted', 'requires a run escalation ladder')
            outputs = state.get('produces', {})
            _mapping(outputs, path+'.produces')
            for artifact, declaration in outputs.items():
                output_path = _at(path+'.produces',artifact)
                _name(artifact, output_path)
                _fields(declaration, {'format'}, {'schema'}, output_path)
                if artifact == 'request':
                    raise _error(output_path, 'cannot produce immutable request')
                if artifact in artifacts and artifacts[artifact] != declaration:
                    raise _error(output_path, 'conflicting artifact format/schema')
                artifacts[artifact] = declaration
                locations[_at('artifacts', artifact)] = output_path
                normalized['produces'].append(artifact)
            states[state_name] = normalized
        run_budget = dict(max_invocations=_positive(limits.get('run_invocations',sum(s.get('budget',{}).get('max_invocations',0) for s in states.values()) or 1),here+'.limits.run_invocations'),
                          on_exceeded=limits.get('on_exceeded','NEEDS_HUMAN'))
        # Every target must have an explicit declaration; names carry no lifecycle meaning.
        targets = [run_budget['on_exceeded']]
        for s in states.values():
            _mapping(s.get('on',{}), here+'.states.on')
            targets.extend(s.get('on',{}).values())
            if 'budget' in s: targets.append(s['budget']['on_exceeded'])
            if 'escalation_exhausted' in s: targets.append(s['escalation_exhausted'])
        for target in targets:
            _name(target, here+'.states.on')
            if target in ('ACCEPTED','REOPENED','COMMIT_BLOCKED'):
                raise _error(here, 'lifecycle results cannot be delivery transitions')
        flows[flow_name] = dict(entry=flow['start'],run_budget=run_budget,states=states)
    try:
        bundle = workflow_bundle_from_data(dict(version=2,profiles=profiles,roles=roles,flows=flows,artifacts=artifacts))
        for flow_name in flows:
            compile_workflow(bundle, flow_name, lambda binding: _NeverExecute())
    except (FlowConfigError, WorkflowCompileError) as exc:
        message = str(exc)
        for old, new in sorted(locations.items(), key=lambda pair:-len(pair[0])):
            message = message.replace(old, new)
        for old,new in (('.command.argv','.command'), ('.profile','.agent'), ('profile','agent'), ('.max_invocations','.invocations'),
                        ('.max_state_visits','.visits'), ('.run_budget','.limits'), ('.entry','.start'),
                        ('.escalation_exhausted','.on_exhausted')):
            message = message.replace(old,new)
        raise FlowConfigError(message[:512]) from None
    _check_inputs(bundle)
    return SimpleConfig(bundle, agents, lifecycle)


def _check_inputs(bundle):
    """A producer must be reachable before the consumer, within the same flow."""
    for flow_name, flow in bundle.flows.items():
        for consumer, state in flow.states.items():
            seen, pending = set(), [flow.entry]
            while pending:
                node = pending.pop()
                if node in seen or node == consumer:
                    continue
                seen.add(node)
                declaration = flow.states[node]
                pending.extend(declaration.on.values())
                if declaration.budget: pending.append(declaration.budget.on_exceeded)
                if declaration.escalation_exhausted: pending.append(declaration.escalation_exhausted)
            ancestors = {consumer}
            while True:
                added = {n for n,s in flow.states.items() if set(s.on.values()) & ancestors}
                if added <= ancestors: break
                ancestors.update(added)
            seen &= ancestors
            for artifact in state.consumes:
                if artifact != 'request' and not any(artifact in flow.states[n].produces for n in seen):
                    raise _error(f'flows.{flow_name}.states.{consumer}.consumes',
                                 f'artifact {artifact!r} has no reachable producer before this state')
