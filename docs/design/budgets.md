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

State-visit admission is checked before allocating a visit or invocation ID. A refused visit
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

RUNTIME-V2-E separates two phases. Check the state-visit limit and existing diagnostic
admission, then commit the visit and emit `state_entered`. Only then check the run
invocation cap and scopes in declaration order. One refusal cause is selected.
Invocation refusal keeps the visit but consumes no invocation capacity. Successful
checks increment all invocation counters together immediately before `invocation_started`.
Executor errors, timeouts, semantic failures and exceptions consume that capacity.
Recorder failures never roll counters back.

Refusal reuses the existing path: clear pending diagnostics, emit `budget_exceeded`,
then select the state's own budget route (or fail with `run_error` when absent).
Fallbacks check their own allowances. An exhausted run invocation cap also refuses
executable fallbacks; terminals remain admissible without invocation capacity.

Metrics are `max_state_visits_per_run`, `max_invocations_per_run`, and
`invocation_scope:<exact name>`. The scope suffix is opaque. `observed` means already
consumed usage, so a refusal at limit 3 reports 3, not 4. A state-visit refusal has no visit/invocation IDs. Invocation refusal has the admitted
visit ID and no invocation ID. Target behavior is described below.
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

Invocation checks now follow state admission (RUNTIME-V2-E). Their refusal consumes
the already admitted visit and no invocation allowance. Visit-limit refusal still
prevents entry entirely. Targets undergo normal independent admission. Existing
counters are reused; no new accounting scopes are introduced.

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

## Two-phase admission (RUNTIME-V2-E)

This intentionally corrects C/D's combined admission boundary. Previously invocation
refusal erased a permitted entry from visit accounting. Now its normal routed sequence is:

```text
state_entered
budget_exceeded      # state_visit_id present, invocation_id absent
transition_selected  # same visit ID, budget trigger, selected target
state_exited         # ordinary monotonic state duration
<target state processing>
```

This contributes one state visit, zero executor invocations, and one refusal to
RunSummary. It creates no invocation duration. Visit-limit refusal retains the old
pre-entry sequence with no IDs. Successful work, executor failure, terminals and
cause routing are unchanged. Invocation counters still commit atomically only after
all invocation checks succeed.

Without a configured refusal route, the admitted visit exits after `budget_exceeded`,
then `run_error` reports the missing route. Recorder failures instead abort immediately,
possibly leaving an incomplete visit; no compensating exits or persistence rollback.
Diagnostic validation remains a pre-entry admission check, now before invocation
capacity checks. Rejected diagnostics do not create a visit.

The updated reader accepts both historical pre-entry and current admitted invocation
refusals, validates visit/target consistency, and distinguishes refusal exits from
terminal exits. Older readers may reject these newly populated existing fields; no
Event field or kind is added. Existing direct callers without invocation refusal keep
their event sequences. Cycles retain the same conservative validation.

A repeated entry whose invocation scope is exhausted now increments the entry-state
visit count before its alternate target runs. This permits an independent entry visit
cap to stop subsequent entries using its own cause route, without new counters.
