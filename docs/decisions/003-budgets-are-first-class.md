# ADR-003: Budgets are first-class control-flow primitives

## Status

Accepted.

## Decision

Budget exhaustion emits explicit runtime events that may participate in state transitions. `budget_exceeded` is never an executor outcome: it means the runtime refuses to start or continue execution because a budget is exhausted. Limit names must identify their scope: invocations per state visit, state visits per run, invocations per state per run, or run-level budgets.

## Why

Budget limits are not merely telemetry.

They determine whether the system should:

- retry;
- fall back;
- escalate;
- fail;
- stop.

## Example

```text
fix --success----------> test
fix --budget_exceeded-> human
```

## Consequence

Adapters may enforce provider-native limits as defense-in-depth, but the runtime owns authoritative limits.
