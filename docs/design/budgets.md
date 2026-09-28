# Budget Design

## Principle

Budget control belongs outside the agent.

A provider's own limit is useful defense-in-depth, but it is not the authoritative runtime boundary.

## Why

An agent cannot be trusted to decide:

- whether another retry is economically justified;
- whether more context is worth loading;
- whether polishing should continue;
- whether a loop should terminate.

Those are control-plane decisions.

## Initial budget dimensions

Start small.

Budgets are outside RUNTIME-001. Early budget names must explicitly identify scope:

- `max_invocations_per_state_visit`: invocations within one visit;
- `max_state_visits_per_run`: visits to a given state within one run;
- `max_invocations_per_state_per_run`: invocations of a given state across its visits;
- `max_invocations_per_run`: invocations across the entire run;
- `invocation_timeout`: deadline for one invocation;
- `max_wall_time_per_run`: wall time for the entire run.

Do not collapse these into an unscoped invocation limit. These names express scope; the configuration schema is not yet frozen.

Then add when reliable usage data exists:

- input tokens;
- output tokens;
- total tokens;
- provider-reported cost;
- estimated cost.

## Event semantics

When the runtime refuses to start or continue execution because a budget is exhausted, it emits an explicit control-plane event (never an executor outcome):

```text
budget_exceeded
```

with structured detail:

```yaml
scope: state_per_run
metric: invocations
limit: 2
observed: 2
state: fix
```

The state machine decides what happens next.

Current supported semantics (RUNTIME-V2-B): `budget_exceeded` may target a
terminal or executable state. The source must have `max_state_visits_per_run`;
all ordinary target and executor validation still applies.

Admission is checked before allocating a visit or invocation ID. A refused visit
emits `budget_exceeded` (metric, limit, observed), then `transition_selected`
(target, trigger_event). Neither has a visit/invocation ID or duration. The target
then undergoes ordinary admission; an executable target emits normal state and
invocation events. If it also refuses, its own handler applies. A missing handler
retains the existing `ExecutionError`/`run_error` behavior. Refusal clears pending
diagnostics, and recorder failures retain their observed-before-persisted boundary.

Cycle validation checks paths that do not consume finite admission allowances:
bounded states contribute refusal edges, unlimited states contribute semantic
edges. This graph must be acyclic, including unreachable components. Thus pure
refusal loops and mixed refusal/unlimited-execution loops are rejected, while
cycles requiring bounded admissions remain valid. No new counters are introduced.
Executors still cannot return `budget_exceeded`.

## No hidden retry

Adapters must not silently retry agent work beyond low-level transport retry.

Semantic retries belong to the flow.

## Accounting hierarchy

The conceptual hierarchy is:

```text
run
  -> state visits
      -> invocations
          -> provider turns
```

Budgets may eventually apply to any level, but the model must keep them distinct.

`timeout` is the executor outcome for an invocation that did not finish by its deadline. RUNTIME-002 defines state-visit admission accounting in [its MicroSpec](../plans/runtime-002.yaml). Cancellation enforcement and deadline-event precedence remain deferred.
