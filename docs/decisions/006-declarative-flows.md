# ADR-006: Delivery flows are declarative data

## Status

Accepted.

## Decision

The runtime executes flow definitions rather than hard-coding one software-delivery process. Every executable flow requires an explicit `initial`. State declaration order has no semantic meaning. A missing transition for an outcome/event is a runtime error, with no implicit retry, fallback, or default transition.

## Why

A fixed workflow such as:

```text
plan -> implement -> review -> retry
```

is useful as a preset but should not define the architecture.

Different work may require:

```text
research -> human -> implement
```

or:

```text
implement -> test -> fix -> test -> accept
```

## Consequence

Existing convenience flows such as `deliver`, `qa-fix`, and `qa-accept` should evolve into presets/thin layers over the generic runtime rather than becoming the runtime itself.
