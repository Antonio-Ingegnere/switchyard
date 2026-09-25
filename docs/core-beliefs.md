# Core Beliefs

## 1. Agents are executors

An agent receives bounded work and returns a normalized result.

It does not own workflow transitions.

## 2. Workflow is data

The delivery flow must be declarative and replaceable.

`implement -> test -> fix -> test -> accept` is a preset, not the architecture.

## 3. Budgets are control flow

A budget is not only accounting metadata.

Budget exhaustion may change the next state:

```text
implement --budget_exceeded--> human
```

## 4. Loops are suspicious by default

Every cycle requires an explicit bound.

Examples:

- max state visits;
- max invocations;
- max repair attempts;
- max wall time;
- max token/cost budget.

## 5. Verification should be deterministic where possible

Prefer:

```text
npm test
pytest
lint
typecheck
schema validation
exit code
structured assertions
```

over:

```text
Ask another LLM whether this seems correct.
```

LLM review may exist, but it is not equivalent to deterministic verification.

## 6. Context is a resource

More context is not automatically better.

Context must be intentionally selected for the current state and task.

## 7. Hidden session state is not durable knowledge

Important project decisions belong in version-controlled documents, code, tests, or explicit runtime state.

## 8. Micro SDD is a useful frontend, not the runtime

A MicroSpec is one small, independently verifiable change contract.

The runtime should also accept other sources of work.

## 9. Small runtime, extensible edges

The core should know as little as possible about:

- providers;
- models;
- CLIs;
- issue trackers;
- SDD methodologies.

Those belong at the edges.

## 10. Cost is an engineering constraint

Token use, wall-clock time, retries, and paid invocations should be observable and bounded.

The system should make waste structurally harder.
