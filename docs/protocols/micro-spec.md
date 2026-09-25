# MicroSpec Protocol

## Purpose

A MicroSpec is the smallest useful change contract that can be independently implemented and verified.

It is intentionally smaller than traditional SDD artifact chains.

## Required fields

```yaml
id: string

goal: |
  What observable outcome should exist?

scope:
  - Files/components/areas that may change.

constraints:
  - Architectural or behavioral constraints.

acceptance:
  - Verifiable acceptance criteria.

verification:
  - How acceptance should be checked.

non_goals:
  - Explicitly excluded work.
```

## Optional fields

```yaml
context:
  - references

budget:
  max_invocations_per_run: 2
  max_wall_time_per_run: 15m

preferred_executor:
  agent: codex
```

Executor preference is advisory unless the flow/policy says otherwise.

## Example

```yaml
id: FLOW-017

goal: |
  Add per-state invocation limits.

scope:
  - runtime/budget
  - runtime/state_machine

constraints:
  - Executors must not choose transitions.
  - Budget logic must remain outside agent adapters.

acceptance:
  - A state can define max_invocations_per_state_per_run.
  - Exceeding the limit emits budget_exceeded.
  - The flow may transition on budget_exceeded.

verification:
  - Unit tests for limit accounting.
  - Integration test for a transition caused by budget exhaustion.

non_goals:
  - Token-based budgets.
  - Global persistence.
  - Provider billing integration.
```

## Sizing rule

A MicroSpec should normally be small enough to fit:

- one bounded implementation cycle;
- one focused verification cycle;
- a reviewable diff.

If not, split it.
