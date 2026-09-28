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
terminal or executable state. The source must have an applicable visit or invocation limit;
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

## Scoped invocation admission (RUNTIME-V2-C)

`Flow.max_invocations_per_run` optionally caps admitted executor invocations.
`State.invocation_budgets` is a defensively copied tuple of frozen
`InvocationBudgetScope(name, limit)` values. Multiple states with the same exact
name share one run-local counter and must declare the same limit. Different names
are independent; duplicate names within a state and scopes on terminals are invalid.
Names are nonempty strings without NUL, with no normalization. Limits are exact
nonnegative integers (not bool); zero refuses the first invocation. A state-local
invocation allowance is simply a uniquely named scope, not a second counter system.

Admission first checks the existing state-visit limit, then the run invocation cap,
then scopes in declaration order. All checks precede admission; one refusal cause
is selected. Refused work consumes neither visits nor invocations. Existing diagnostic
admission follows successful budget checks. After `state_entered`, all invocation
counters increment together immediately before `invocation_started`. Executor errors,
timeouts, semantic failures and exceptions consume capacity. Recorder failures do not
roll it back: an observed invocation start may fail persistence before executor entry.

Refusal reuses the existing path: clear pending diagnostics, emit `budget_exceeded`,
then select the state's own budget route (or fail with `run_error` when absent).
Fallbacks check their own allowances. An exhausted run invocation cap also refuses
executable fallbacks; terminals remain admissible without invocation capacity.

Metrics are `max_state_visits_per_run`, `max_invocations_per_run`, and
`invocation_scope:<exact name>`. The scope suffix is opaque. `observed` means already
consumed usage, so a refusal at limit 3 reports 3, not 4. The budget event has no
visit/invocation IDs or target; its following `transition_selected` carries the target.
Event fields and recorder format are unchanged. The historical reader accepts these
specific metrics while retaining strict rejection of unknown metrics.

Invocation caps do not relax existing cycle rules. Executable cycles still require
visit-bounded admission; all refusal edges participate in cycle safety checking.
Whole-run state-visit caps are not implemented. The V2 compiler remains deferred.

## Cause-aware refusal routes (RUNTIME-V2-D)

Three optional fields select routes independently:

- `Flow.max_invocations_exceeded`: target for the run invocation cap; requires a
  configured cap and an existing terminal target.
- `State.state_visit_budget_exceeded`: target for that state's visit cap; requires
  a configured cap and an existing terminal or executable target.
- `InvocationBudgetScope.on_exceeded`: target for that named scope; may be executable
  or terminal. Every declaration of the same scope must agree on both limit and route,
  including the absence of a route.

The first failed check still wins: visits, run invocations, then scopes in declaration
order. Its specific route takes precedence; absent one, `State.on['budget_exceeded']`
remains the fallback. If neither exists, the existing missing-transition error applies.
Internal immutable refusal values capture the selected configuration directly. Metric
strings are serialized evidence, never parsed to select a route.

All checks still precede state admission. In particular, an invocation-scope refusal
allocates no visit ID and consumes no visit or invocation allowance. This preserves
RUNTIME-V2-C; it does not create a partially admitted visit. Targets undergo normal
independent admission. No new counters or accounting rules are introduced.

Specific-route `budget_exceeded` events populate the existing `target` field; the
following `transition_selected.target` agrees. Legacy fallback retains its historical
budget event with `target=None`, followed by the target-bearing transition. Event
fields/kinds, metrics, consumed `observed` values and recorder failure boundaries are
unchanged. The reader accepts both shapes and rejects disagreement between recorded
refusal and transition targets. Older reader versions may reject the new populated
field; historical logs remain readable with the updated reader.

Cycle validation includes all specific and legacy refusal edges, even conservatively
shadowed routes, while retaining existing visit-bound semantic-cycle rules. Run-specific
routes are terminal. Scope and visit routes cannot introduce unbounded refusal cycles.

This closes the distinction between an entry refusal's route and a local invocation
scope's route. It does not make runtime state visits independent of invocation admission:
a future compiler must still account for that distinction when modeling logical entries.
