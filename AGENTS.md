# Repository Instructions

This repository is intended to implement a deterministic control plane for bounded AI-agent execution.

The system must remain agent-agnostic. Codex, Claude Code, local models, and future agents are adapters/drivers behind a stable runtime contract.

## Read first

Before making architectural changes, read:

- `docs/vision.md`
- `docs/core-beliefs.md`
- `docs/architecture.md`
- `docs/vocabulary.md`

Read only the relevant design documents for the task:

- State machine: `docs/design/state-machine.md`
- Budgets: `docs/design/budgets.md`
- Context construction: `docs/design/context-engine.md`
- Verification: `docs/design/verification.md`
- Agent adapters: `docs/protocols/agent-driver.md`
- Micro specs: `docs/protocols/micro-spec.md`

Architectural decisions live in `docs/decisions/`.

## Critical invariants

1. Executors never choose workflow transitions.
2. The runtime/state machine owns control flow.
3. Every cycle must have an explicit deterministic bound.
4. Budget exhaustion is an explicit workflow event.
5. `run`, `state visit`, `invocation`, `turn`, and `attempt` are distinct concepts.
6. Agent-specific behavior belongs in adapters, not in core workflow semantics.
7. Verification should be deterministic whenever practical.
8. Context is explicitly built for each invocation.
9. Durable knowledge belongs in the repository, not only in model/session memory.
10. Delivery workflows are presets over the generic runtime, not hard-coded into the runtime.

## Scope discipline

Do not turn this into a generic "multi-agent collaboration" framework.

The target is a small control-plane runtime for expensive, probabilistic executors.

Avoid:

- autonomous agent societies;
- agents negotiating workflow transitions;
- unbounded planner/worker loops;
- hidden retry logic inside adapters;
- provider-specific types leaking into the core;
- large generated SDD document chains;
- framework features without a concrete use case.

## Expected development style

Work in small, independently verifiable changes.

A good task should normally fit into one MicroSpec and one bounded execution cycle.

Do not implement the entire architecture at once. Prefer a vertical slice that proves a runtime invariant end-to-end.
