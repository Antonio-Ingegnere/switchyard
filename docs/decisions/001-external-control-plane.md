# ADR-001: Workflow control is external to agents

## Status

Accepted.

## Decision

The runtime/state machine owns workflow transitions, retries, stopping conditions, and escalation.

Agents execute bounded work and return normalized results.

## Why

Giving workflow ownership to the same probabilistic executor that performs the task makes execution hard to reason about and hard to bound.

External control enables:

- deterministic stopping;
- observable retries;
- consistent budgets;
- interchangeable providers;
- reproducible flow semantics.

## Rejected

Agent returns:

```yaml
status: success
next_state: review
```

## Accepted

Agent returns:

```yaml
status: success
```

The state machine evaluates:

```text
implement.success -> review
```
