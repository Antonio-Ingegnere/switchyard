# ADR-004: Every cycle must be bounded

## Status

Accepted.

## Decision

A flow containing a cycle must have an explicit deterministic bound.

## Why

Open-ended repair/review loops are a primary source of runaway token use and unpredictable execution.

## Valid bounds

Examples:

- maximum state visits per run;
- maximum invocations per state per run;
- maximum invocations per run;
- maximum attempts;
- run wall time;
- cost/token budget.

## Consequence

RUNTIME-001 rejects all cycles before execution. Bounded-cycle support is deferred to the budget-engine slice, which must ensure that every allowed cycle has a deterministic bound.
