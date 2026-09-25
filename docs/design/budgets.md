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

Current supported semantics (RUNTIME-002): `budget_exceeded` may transition
only to a terminal state. The target name does not determine whether it is terminal.

```text
budget_exceeded -> human   # human must be terminal
budget_exceeded -> failed  # failed must be terminal
```

`fallback_agent` and all other nonterminal budget transitions are future extensions
and are not currently supported. Flow validation rejects them.

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
