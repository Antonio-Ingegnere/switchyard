# ADR-005: MicroSpec is a frontend protocol, not the core runtime

## Status

Accepted.

## Decision

MicroSpec is a recommended way to describe small work units.

The runtime itself accepts normalized Work Units and is not coupled to SDD.

## Why

The useful part of Micro SDD is the bounded, independently verifiable change.

The runtime should not require:

- spec.md;
- plan.md;
- research.md;
- tasks.md;
- design.md

for every change.

## Consequence

Other work sources may be added later:

- GitHub issue;
- ADO work item;
- CLI task;
- API request.
