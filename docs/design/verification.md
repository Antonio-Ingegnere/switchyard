# Verification Design

## Principle

A result is not accepted merely because an agent says it is done.

## Verification order

Prefer the narrowest deterministic check that proves the acceptance criterion.

Examples:

1. focused unit test;
2. targeted integration test;
3. typecheck/lint;
4. broader test suite when justified.

## Normalized result

```yaml
status: success | failed | error | timeout | cancelled
check: pytest
exit_code: 0
duration_ms: 1840
artifacts:
  - path/to/report
```

Verifier executor outcomes use the shared vocabulary. Check-specific details remain metadata; they do not introduce alternative transition outcome names. `timeout` means the invocation missed its configured deadline.

## LLM review

LLM review is useful for:

- design critique;
- semantic review;
- missing requirements;
- readability.

It must be represented honestly as probabilistic review.

Do not label LLM judgement as deterministic verification.

## Acceptance

A flow preset may require both:

```text
deterministic checks pass
AND
review state approves
```

but these remain separate concepts.
