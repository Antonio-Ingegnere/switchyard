# Context Engine Design

## Principle

Context is explicitly assembled for an invocation.

Before the full Context Engine exists, every invocation must still receive an explicit immutable Work Unit payload. Provider session history must not implicitly become required context.

The runtime should not assume that a long-lived agent session is the authoritative project memory.

## Sources

Possible context sources:

- MicroSpec;
- repository `AGENTS.md`;
- selected architecture documents;
- selected ADRs;
- source files;
- previous normalized execution results;
- verifier failures.

## Selection

Each state should request only the context it needs.

Example:

```yaml
implement:
  context:
    - work_unit
    - repository_instructions
    - relevant_source_files

fix:
  context:
    - work_unit
    - previous_diff
    - failing_verification
```

## Durable versus ephemeral

Durable:

- project architecture;
- invariants;
- ADRs;
- accepted requirements.

Ephemeral:

- current failure output;
- current diff;
- temporary plan;
- provider session metadata.

Do not confuse them.

## Provider-native sessions

Adapters may use provider-native session/thread continuation for efficiency.

However:

- the runtime must be able to reconstruct required context without it;
- provider session state must not become the only copy of an important decision;
- session IDs are execution metadata, not architecture.
