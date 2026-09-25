# Bootstrap Plan

This is intentionally not a full implementation plan.

Build the smallest vertical slice that proves the architecture.

## Phase 0 — Decide implementation substrate

The implementation language is a separate architectural decision. The existing prototype is Python, so Python is the baseline unless a concrete advantage for the first runtime slice justifies another language. TypeScript is not selected.

RUNTIME-001 needs no serialized configuration format or persistence format. Decide those when the corresponding slices require them.

Do not introduce infrastructure services for the MVP.

## Phase 1 — Smallest runtime (RUNTIME-001)

Implement only:

1. immutable Work Unit payload;
2. declarative flow with required `initial`, states, transitions, and terminals;
3. normalized ExecutorResult and Executor interface;
4. deterministic fake executor;
5. pure state-machine runner;
6. in-memory execution events with distinct run, state visit, and invocation IDs.

Prove:

```text
declarative flow -> executor result -> runtime-owned transition -> terminal state
```

Validate before execution and reject all cycles. Missing transitions are explicit runtime errors. No subprocesses, persistence, retries, budgets, or deadline enforcement belong in this slice. The reviewable MicroSpec is in `examples/micro-spec.yaml`.

Later phases below describe deferred architectural direction, not approved implementation scope. A command executor and persistent event storage require separate MicroSpecs before implementation.

## Phase 2 — Bounds

Add:

- max state visits per run;
- max invocations per state visit;
- max invocations per state per run;
- run-level budgets;
- invocation timeout;
- `budget_exceeded` event.

Prove a bounded cycle:

```text
test -> fix -> test
```

using fake executors.

## Phase 3 — First agent driver

Add one real coding-agent adapter.

Prefer the easiest reliable integration in the target environment.

Keep the driver thin.

Prove:

```text
MicroSpec -> implement(agent) -> verify(command) -> accept
```

## Phase 4 — Second provider

Add the second agent driver without changing core flow semantics.

This is the architecture test.

If core abstractions must be rewritten heavily to add the second provider, the boundary is wrong.

## Phase 5 — Context engine

Add explicit context selection and recording.

Do not begin with embeddings, vector databases, or automated repository-wide context discovery.

Start with explicit references.

## Phase 6 — Presets

Represent delivery flows as reusable declarative presets.

Potential preset:

```text
deliver -> verify -> accept
              \-> fix -> verify
                        \-> escalate
```

## Explicitly postponed

Do not implement until a concrete need appears:

- distributed runtime;
- remote workers;
- web UI;
- vector database;
- autonomous planner;
- organization simulation;
- marketplace;
- complex scheduling;
- full cost optimization strategy;
- plugin ecosystem.
