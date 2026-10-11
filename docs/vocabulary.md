# Vocabulary

Precise terminology matters because several concepts look similar but have different budget semantics.

## Work Unit

The normalized unit of requested work.

Examples:

- one MicroSpec;
- one issue;
- one CLI task.

## Run

One top-level execution of a Work Unit through a flow.

A run starts once and finishes once.

## State

A node in a flow definition.

Examples:

- `implement`
- `test`
- `review`
- `fix`
- `human`

## State Visit

One entry into a state during a run.

The same state may be visited more than once.

## Invocation

One call to an executor during a state visit.

Examples:

- one Codex execution;
- one Claude Code execution;
- one deterministic command execution.

## Turn

A provider/model-level interaction inside an invocation.

Not every adapter can expose turns in the same way.

## Infrastructure failure

A provider process that ended before returning a valid StageResult (exit, signal,
timeout, transport/start failure, invalid result). It is not an invocation charge:
semantic budgets ignore it, and `agents.<name>.infrastructure_failures` bounds it.

## Attempt

A semantic retry of a piece of work.

Do not use this word as a substitute for invocation unless the flow explicitly defines them as equivalent.

## Executor

Anything that performs work for a state.

Examples:

- agent driver;
- shell command;
- verifier;
- human gate.

## Agent Driver

A provider-specific adapter that converts a normalized `AgentRequest` into provider execution and returns a normalized `AgentResult`.

## Transition

A runtime-owned decision selecting the next state from the current state and event/result.

## Budget

A limit on a measurable runtime resource.

Examples:

- invocations;
- state visits;
- wall time;
- tokens;
- monetary cost.

## Gate

A check that must produce a normalized result before a transition can proceed.

## MicroSpec

A small change contract with enough information to implement and verify one bounded unit of work.
