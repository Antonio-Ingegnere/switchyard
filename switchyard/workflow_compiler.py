"""Opt-in compilation of validated V2 bundles to ordinary runtime graphs."""
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

from runtime import Executor, Flow, FlowValidationError, InvocationBudgetScope, State, validate
from .workflow_bundle import (CAPABILITIES, ESCALATION_TRIGGERS, ArtifactSpec, Effort, ExecutionProfile, Provider, WorkflowBundle,
                              WorkflowStateSpec)


class WorkflowCompileError(ValueError):
    """A binding cannot be resolved or a plan cannot be represented by runtime."""


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    flow_name: str
    logical_state: str
    role_name: str
    rung_index: int
    profile_name: str
    provider: Provider
    model: str | None
    effort: Effort | None
    escalation_kind: str | None
    context_mode: str | None
    executor_allowed_outcomes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] | None = None

    def __post_init__(self):
        object.__setattr__(self, 'executor_allowed_outcomes', tuple(self.executor_allowed_outcomes))
        if self.capabilities is not None:
            if any(c not in CAPABILITIES for c in self.capabilities):
                raise ValueError('unknown execution capability')
            object.__setattr__(self, 'capabilities', tuple(c for c in CAPABILITIES if c in self.capabilities))


ExecutorResolver = Callable[[ExecutionBinding], Executor]


@dataclass(frozen=True, slots=True)
class CompiledStatePlan:
    logical_state: str
    binding: ExecutionBinding | None
    declaration: WorkflowStateSpec
    state: State

    @property
    def executor_allowed_outcomes(self) -> tuple[str, ...]:
        return () if self.binding is None else self.binding.executor_allowed_outcomes


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    flow: Flow
    executors: Mapping[str, Executor]
    plan: Mapping[str, CompiledStatePlan]
    entry_states: Mapping[str, str]
    artifacts: Mapping[str, ArtifactSpec] = field(default_factory=dict)
    profiles: Mapping[str, ExecutionProfile] = field(default_factory=dict)

    def __post_init__(self):
        for name in ('executors', 'plan', 'entry_states', 'artifacts', 'profiles'):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


def _state_id(flow: str, logical: str, index: int) -> str:
    return f'@v2:{flow}:{logical}:{index}'


def _scope_id(flow: str, logical: str, index: int | None = None) -> str:
    suffix = 'state' if index is None else f'rung:{index}'
    return f'@v2-scope:{flow}:{logical}:{suffix}'


def compile_workflow(bundle: WorkflowBundle, flow_name: str,
                     executor_resolver: ExecutorResolver) -> CompiledWorkflow:
    """Compile a loader-validated bundle; resolve each rung once without invoking it.

    Runtime validation is authoritative, including its conservative cycle rules.
    No extra caps or execution wrappers are invented to make an invalid graph pass.
    """
    source = bundle.flow(flow_name)
    identities: dict[str, tuple[str, ...]] = {}
    used_states: set[str] = set()
    for name, spec in sorted(source.states.items()):
        ids = (name,) if spec.terminal or spec.wait_for else tuple(
            _state_id(flow_name, name, i) for i in range(1 + len(bundle.roles[spec.role].escalation)))
        for identifier in ids:
            if identifier in used_states:
                raise WorkflowCompileError(f'generated runtime state collision: {identifier}')
            used_states.add(identifier)
        identities[name] = ids
    entries = {name: ids[0] for name, ids in identities.items()}
    states: dict[str, State] = {}
    executors: dict[str, Executor] = {}
    plan: dict[str, CompiledStatePlan] = {}
    used_scopes: set[str] = set()

    def scope(name: str, limit: int, target: str | None, per_visit_of: str | None = None) -> InvocationBudgetScope:
        if name in used_scopes:
            raise WorkflowCompileError(f'generated invocation scope collision: {name}')
        used_scopes.add(name)
        return InvocationBudgetScope(name, limit, target, per_visit_of)

    for name, spec in sorted(source.states.items()):
        if spec.wait_for:
            state = State(wait_for=spec.wait_for, resume_to=entries[spec.resume_to],
                          max_state_visits_per_run=spec.budget.max_state_visits,
                          state_visit_budget_exceeded=entries[spec.budget.on_exceeded])
            states[name] = state
            plan[name] = CompiledStatePlan(name, None, spec, state)
            continue
        if spec.terminal:
            state = State(terminal=True)
            states[name] = state
            plan[name] = CompiledStatePlan(name, None, spec, state)
            continue
        role = bundle.roles[spec.role]
        shared = None
        if spec.budget is not None and spec.budget.max_invocations is not None:
            shared = scope(_scope_id(flow_name, name), spec.budget.max_invocations,
                           entries[spec.budget.on_exceeded])
        for index, declaration in enumerate((role.primary, *(step.binding for step in role.escalation))):
            identifier = identities[name][index]
            step = role.escalation[index-1] if index else None
            next_step = role.escalation[index] if index < len(role.escalation) else None

            def target(outcome: str) -> str | None:
                if next_step is not None and outcome in next_step.on:
                    return identities[name][index+1]
                if outcome in spec.on:
                    return entries[spec.on[outcome]]
                if role.escalation and outcome in ESCALATION_TRIGGERS:
                    return entries[spec.escalation_exhausted]
                return None

            outcomes = set(spec.on)
            if role.escalation:
                outcomes.update(ESCALATION_TRIGGERS)
            outcomes.discard('budget_exceeded')  # Runtime control fact, never an executor result.
            transitions = {outcome: target(outcome) for outcome in sorted(outcomes)}
            if declaration.outcomes is not None:
                if not declaration.outcomes:
                    raise WorkflowCompileError(f'{flow_name}.{name} rung {index}: empty outcomes restriction')
                invalid = set(declaration.outcomes) - transitions.keys()
                if invalid:
                    raise WorkflowCompileError(
                        f'{flow_name}.{name} rung {index}: unroutable outcomes {sorted(invalid)!r}')
                transitions = {key: value for key, value in transitions.items()
                               if key in declaration.outcomes}
            local = scope(_scope_id(flow_name, name, index), declaration.budget.max_invocations,
                          target('budget_exceeded'), entries[name] if spec.rung_budget_scope == 'visit' else None)
            visit_limit = spec.budget.max_state_visits if index == 0 and spec.budget else None
            state = State(identifier, transitions, max_state_visits_per_run=visit_limit,
                          invocation_budgets=((shared,) if shared else ()) + (local,),
                          state_visit_budget_exceeded=entries[spec.budget.on_exceeded]
                          if visit_limit is not None else None)
            profile = bundle.profiles[declaration.profile]
            binding = ExecutionBinding(flow_name, name, spec.role, index, declaration.profile,
                                       profile.provider, profile.model, declaration.effort,
                                       step.kind if step else None, step.context if step else None,
                                       tuple(sorted(key for key in state.on if key != 'budget_exceeded')),
                                       spec.capabilities)
            try:
                executor = executor_resolver(binding)
            except Exception as exc:
                raise WorkflowCompileError(f'cannot resolve {flow_name}.{name} rung {index}') from exc
            if not callable(getattr(executor, 'run', None)):
                raise WorkflowCompileError(f'resolver returned invalid executor for {flow_name}.{name} rung {index}')
            states[identifier] = state
            executors[identifier] = executor
            plan[identifier] = CompiledStatePlan(name, binding, spec, state)
    flow = Flow(entries[source.entry], states, source.run_budget.max_invocations,
                entries[source.run_budget.on_exceeded])
    try:
        validate(flow, executors)
    except FlowValidationError as exc:
        raise WorkflowCompileError(f'cannot represent flow {flow_name!r}: {exc}') from exc
    return CompiledWorkflow(flow, executors, plan, entries, bundle.artifacts, bundle.profiles)
