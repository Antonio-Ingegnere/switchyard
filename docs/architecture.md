# Architecture

## High-level model

```text
                     Work Source
      MicroSpec / Issue / CLI / API / Human
                         |
                         v
                    Work Unit
                         |
                         v
              +--------------------+
              |   Flow Runtime     |
              |   State Machine    |
              +---------+----------+
                        |
          +-------------+-------------+
          |             |             |
          v             v             v
      Budget        Context       Verification
      Engine        Engine          Engine
          \             |             /
           \            |            /
            +-----------+-----------+
                        |
                        v
                  Agent Gateway
            +-----------+-----------+
            |           |           |
            v           v           v
          Codex       Claude      Future
          Driver      Driver      Driver
                        |
                        v
                Normalized Result
                        |
                        v
                  State Transition
```

## Major components

### Flow Runtime

Responsibilities:

- load a flow definition;
- track current state;
- evaluate transition rules;
- emit execution events;
- stop deterministically.

It must not contain Codex- or Claude-specific logic.

### Budget Engine

Responsibilities:

- track usage at multiple scopes;
- evaluate limits before and after invocation;
- emit normalized budget events.

Possible scopes:

```text
invocation
state visit
state
agent
task/work unit
run
global
```

The initial implementation does not need every scope.

### Context Engine

Responsibilities:

- assemble the minimum context needed by a state;
- resolve references to project knowledge;
- avoid accidental inheritance of unrelated conversation/session context;
- record what context was supplied.

### Agent Gateway

A registry and dispatch layer for agent drivers.

It does not select workflow transitions.

It normalizes provider-specific behavior into `AgentResult`.

### Verification Engine

Runs deterministic checks and normalizes results.

Possible checks:

- command exit code;
- test suite;
- linter;
- type checker;
- file/schema assertion;
- custom verifier.

LLM-based review may be represented as an executor state, not disguised as deterministic verification.

### Event Store

Records runtime events such as:

```text
run_started
state_entered
invocation_started
invocation_finished
verification_finished
budget_exceeded
transition_selected
state_exited
run_finished
```

The first implementation may use an append-only local file or SQLite.

## Dependency direction

Core runtime may depend on stable interfaces.

Provider adapters depend on provider tooling.

Provider-specific code must not be imported by the state machine.

```text
core <- interfaces <- adapters
```

not:

```text
core -> claude-specific workflow logic
```

## Presets

Presets are reusable flow definitions.

Example software-delivery preset:

```text
deliver
   |
   v
verify
 /   \
pass fail
 |    |
 v    v
accept fix
       |
       v
     verify
       |
   bounded retry
       |
       v
    escalate
```

Presets must be removable without changing the runtime.

### Source resolution

Before V2 context/executor compilation, the application control plane resolves
explicit supported request references through a small source resolver protocol.
The GitHub REST edge is the first implementation; neither runtime/state-machine
code nor provider adapters detect URLs or fetch sources. Immutable normalized source
artifacts ground the workflow entry stage. See [source lifecycle and bounds](design/context-engine.md#external-request-sources-source-resolution-001).
