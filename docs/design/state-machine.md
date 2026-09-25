# State Machine Design

## Principle

The state machine is the sole owner of workflow transitions.

Executors return facts/results.

The ExecutorResult contract has no `next_state`. Only the runtime maps outcomes and events to transitions.

## Flow example

```yaml
initial: implement

states:
  implement:
    executor:
      type: agent
      agent: codex
    on:
      success: test
      timeout: human
      budget_exceeded: human

  test:
    executor:
      type: command
      command: pytest -q
    on:
      success: accept
      failed: fix

  fix:
    executor:
      type: agent
      agent: claude
    limits:
      max_state_visits_per_run: 2
    on:
      success: test
      budget_exceeded: human

  accept:
    terminal: true

  human:
    terminal: true
```

The schema is illustrative, not frozen. This bounded-cycle example describes a later slice; RUNTIME-001 rejects all cycles.

## Required properties

A flow must make it possible to determine:

- an explicit required `initial` referencing an existing state;
- valid states;
- terminal states;
- executor for each executable state;
- transition rules;
- relevant bounds.

## Validation

Flow definitions must be validated before any executor is called. State declaration order has no semantic meaning. Terminal states never invoke an executor.

Minimum validation:

- referenced states exist;
- at least one terminal state exists;
- invalid executor definitions are rejected;
- all cycles are rejected in RUNTIME-001, including self-loops and cycles outside the initial path.

## Cycle rule

A cycle is allowed only if at least one deterministic bound limits repeated execution.

Examples:

```text
max_state_visits_per_run
max_invocations_per_state_per_run
max_invocations_per_run
run wall time
run cost
```

RUNTIME-001 prohibits every cycle. Bounded-cycle support belongs to a later slice with the budget engine; it must ensure that every allowed cycle is bounded.

## Transition input

Executor outcomes use exactly this vocabulary:

```text
success
failed
error
timeout
cancelled
```

`timeout` means a particular invocation did not finish within its configured deadline. The outcome vocabulary includes it from the start; RUNTIME-001 does not implement deadlines.

`budget_exceeded` is a control-plane event, never an executor outcome. It is emitted when the runtime refuses to start or continue execution because a budget is exhausted.

A missing transition for the received outcome/event is an explicit runtime error. There are no implicit retries, fallbacks, or default transitions.

Provider-specific errors must be normalized before reaching transition logic.
