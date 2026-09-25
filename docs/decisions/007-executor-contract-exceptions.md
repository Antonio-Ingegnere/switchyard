# ADR-007: Escaped executor exceptions abort the run

## Status

Accepted.

## Decision

`ExecutorResult("success" | "failed" | "error" | "timeout" | "cancelled")`
represents normal executor-contract outcomes. The runtime may select declared
transitions for these outcomes.

Each Executor or AgentDriver must normalize expected operational failures into
ExecutorResult. An exception escaping the executor invocation boundary indicates
an executor implementation failure or a contract violation.

An escaped exception:

- immediately aborts the run through ExecutionError;
- is not automatically converted into the outcome `error`;
- does not participate in ordinary workflow transitions;
- must not trigger an automatic runtime retry.

This applies even if the state declares a transition for `error`.

The conceptual boundary called `Executor.execute()` in the decision discussion
is currently named `Executor.run(work_unit)` in RUNTIME-001. This ADR specifies
the behavior of that boundary and does not rename the interface.

## Why

Adapter bugs and programmer errors must not become workflow events that cause
the runtime to repeatedly invoke a broken executor and consume budget.

## Consequences

Executor implementations distinguish expected operational failures from defects
that escape their contract. The runtime preserves the escaped exception as the
cause of ExecutionError and makes the collected in-memory events available.

`budget_exceeded` remains a runtime event, never an executor outcome.
