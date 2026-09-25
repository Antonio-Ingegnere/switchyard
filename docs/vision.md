# Vision

## One-line definition

A deterministic control plane for interchangeable, expensive, probabilistic coding agents.

## Problem

Coding agents are useful executors, but they are poor owners of control flow.

If workflow logic, retries, stopping conditions, context growth, and budgets are delegated to the same model that performs the work, execution becomes difficult to reason about and easy to overspend.

Existing agent frameworks frequently impose one of two things:

1. a fixed delivery flow such as plan -> implement -> review -> retry; or
2. a generic multi-agent abstraction that gives agents too much autonomy.

This project aims for a smaller primitive:

> Declarative workflow + deterministic runtime + bounded agent invocations.

## Intended shape

Inputs such as a MicroSpec, issue, CLI task, or API request are normalized into a Work Unit.

The runtime executes that work through a declarative state machine.

States may invoke:

- a coding agent;
- a deterministic command;
- a verifier;
- a human approval step;
- a future external executor.

Agent implementations are replaceable drivers.

The runtime owns:

- transitions;
- budgets;
- stopping conditions;
- context assembly;
- execution history;
- verification policy.

## What success looks like

A user can describe a flow such as:

```text
implement -> test -> review -> accept
               \-> fix -> test
```

and independently select Codex, Claude Code, or another executor for individual states.

The runtime guarantees that every cycle is bounded and that executor behavior cannot silently override workflow semantics.

## Non-goals

This project is not:

- a chat product;
- an autonomous software company;
- an agent social network;
- a replacement for GitHub, ADO, or issue trackers;
- a methodology generator;
- a requirement to use Micro SDD;
- a system where an LLM is trusted to enforce its own budget.
