# ADR-002: Agents are interchangeable drivers

## Status

Accepted.

## Decision

Codex, Claude Code, and future executors are integrated through provider-specific drivers behind a stable normalized contract.

## Why

Provider behavior changes faster than workflow semantics.

The core must not depend on one vendor's:

- CLI flags;
- session model;
- tool naming;
- event schema;
- token accounting.

## Consequence

Provider-specific features are exposed through capabilities and adapter-local extensions where necessary.

They must not leak into state-machine semantics.
