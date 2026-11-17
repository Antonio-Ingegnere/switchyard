# Pricing registry and cost attribution (ECONOMICS-004, #2)

```
switchyard work costs WORK [--json] [--run R] [--state S] [--agent A] [--provider P] [--model M] [--invocation ID|I<n>]
switchyard.economics.read_costs(work_root, ...) / switchyard.economics.aggregate(records)
```

`work costs` reports the **estimated list-price cost of provider-reported usage** ([usage](usage.md)) for each
invocation. Each estimate is priced with rates frozen when the invocation ended. It is never an invoiced or billed
amount: `billed_cost` is always unavailable, and a subscription plan may bill nothing per token. It is
observational: it never changes execution, retries, budgets or recovery, and nothing calls a pricing API.

## Registry

The registry has two sources, and nothing is fetched at runtime:

- **Built-in catalog:** `switchyard/pricing_catalog.json`, revision `2026-11-13.1`, checked in.
- **User catalog:** an optional explicit catalog named by `SWITCHYARD_PRICING_CATALOG`, in the same format.

The format is:

```json
{"catalog": "...", "schema_version": 1, "revision": "2026-11-13.1", "currency": "USD", "unit": "per_million_tokens",
 "defaults": {"effective_from": "2026-11-13", "effective_until": null, "verified": "2026-11-13",
              "sources": {"claude": "https://...", "codex": "https://..."}, "basis": {"claude": "...", "codex": "..."}},
 "entries": [{"provider": "claude", "model": "claude-sonnet-5-5", "pricing": "flat",
              "rates": {"input": "2", "cache_write_5m": "2.50", "cache_write_1h": "4", "cache_read": "0.10", "output": "10"}},
             {"provider": "codex", "model": "gpt-5.3-codex", "pricing": "flat", "cache_write": "not_billed",
              "rates": {"input": "1.75", "cache_read": "0.175", "output": "14.00"}},
             {"provider": "claude", "model": "claude-haiku-5-5", "pricing": "per_request_context_tiered"}]}
```

### Entry rules

- **Rates** are decimal strings, never floats.
- **Provenance:** every entry carries a source (an https URL), a verification date, an applicability window
  (`effective_from` inclusive, `effective_until` exclusive) and a basis label. A defect in any entry rejects the
  whole catalog.
- **Precedence:** for a given provider and model, the user catalog replaces the built-in entries. Within the
  deciding catalog, exactly one entry must apply on the invocation's UTC end date.

### Lookup results

| Result | Meaning |
|---|---|
| `price_not_configured` | No entry for this exact model ID. Aliases such as `sonnet` are never resolved, and a new model never inherits its predecessor's price. |
| `price_not_effective` | No entry applies on that date. |
| `ambiguous_price` | More than one entry applies. |
| `tiered_pricing_unresolvable` | The model is known but billed per request by prompt length (Claude Haiku 5.5 above 100K; gpt-5.4, gpt-5.5 and GPT-6 above 272K). Aggregate usage cannot prove which tier applies. |

### Built-in coverage

Rates were verified on 2026-11-13 against the Standard tier of the official pages:

- **Claude** ([Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing); IDs from the
  [models overview](https://platform.claude.com/docs/en/about-claude/models/overview)):
  `claude-fable-5-2`, `claude-fable-5-1`, `claude-opus-5-5` and `claude-sonnet-5-5`. `claude-haiku-5-5` is listed as
  tiered.
- **Codex** ([OpenAI pricing](https://developers.openai.com/api/docs/pricing)): `gpt-5`, `gpt-5.1`, `gpt-5.2`,
  `gpt-5.3-codex`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5.4-mini` and `gpt-5.4-nano`. These have flat rates and no
  cache-write charge. gpt-5.4, gpt-5.5, gpt-6-sol and gpt-6.1-sol are listed as tiered.
- **Basis:** first-party API, global or standard processing. This excludes Batch, Fast or priority processing,
  regional data residency, and partner clouds such as Bedrock or Vertex.

Models whose IDs could not be verified are absent rather than guessed. Changing the catalog is a reviewed code change
with a new revision.

## Frozen snapshots and assessments

When an invocation ends, the diagnostics writer's lifecycle publishes two records after the usage record:

- **`<work>/economics/pricing/<inv>.json`** (record schema 1): the pricing snapshot. It holds:
  - the exact catalog entries the invocation's models resolve to: rates, unit, currency, window, source,
    verification date, basis, and catalog origin and revision;
  - unresolved models with their reasons;
  - the registry SHA-256 and each catalog's revision and SHA-256 (the user catalog's path is never stored);
  - the selection basis (UTC end date; reported model, else the exact requested model; user catalog over built-in;
    aliases never resolved);
  - `quantities`: the frozen chargeable quantities the assessment prices, linked to the usage record by `usage_id`.
    These are the cost scope, the items `{model, category, tokens, reason}` (including Claude's 5m/1h cache-write
    split), the assumptions, and any `blocking_reason` such as a pricing modifier outside the basis;
  - `snapshot_id`, the SHA-256 of the content.
- **`<work>/economics/costs/<inv>.json`** (record schema 1): the cost assessment. It holds:
  - `pricing_snapshot_id`, `cost_scope`, `status` (`complete`, `partial`, `unavailable` or `not_applicable`),
    `reasons` and `assumptions`;
  - `lines` of `{model, category, tokens, rate, amount}` and `unpriced` items of `{model, category, tokens, reason}`;
  - `estimated_amount` and `observed_by`.

Both are immutable: published with fsync and an exclusive link, never overwritten, at most 32 KiB, and containing
only counts, rates and code-owned labels. Changing the current catalog never changes an existing assessment; a test
checks the bytes are identical. Reads verify each snapshot against its id and report `snapshot_integrity`.
Revaluation at other prices is not supported. If it is added, it must be a separate assessment.

### Recovery

- **Crash after the snapshot, before the assessment:** the next drive assesses that snapshot's frozen quantities at
  its frozen rates (`observed_by: recovery_backfill`). These are the same inputs the uninterrupted assessment uses,
  so the lines, status and amount are identical. Recovery never reads the current catalog and never reconstructs a
  quantity; a snapshot that fails verification or belongs to another usage record counts as missing.
- **Crash before the snapshot:** `pricing_not_captured`. The current catalog is never applied to an earlier
  invocation, and no provider is rerun.
- **Restart:** idempotent.
- **Works created before 2.7.0:** they read as `historical_pricing_not_captured`.

## Token normalization

| | Claude | Codex |
|---|---|---|
| Billed quantities | input (excludes cache), cache read, 5m and 1h cache writes, output | uncached input = input − cached input, cached input, output |
| Cache writes | priced only with the documented `usage.cache_creation` split (`ephemeral_5m/1h_input_tokens`, which must sum to `cache_creation_input_tokens`); otherwise `cache_write_duration_not_reported` | not billed for the catalogued models |
| Inconsistent counters | — | `cached_input_exceeds_input` or `input_split_not_reported`: the input is unpriced |
| Reasoning | — | inside output; never priced again |
| Modifiers | top-level `service_tier`, `speed` or `inference_geo` outside the standard basis gives `pricing_modifier_outside_catalog_basis`; when absent, the assumption is recorded | standard tier assumed (recorded) |
| Model | the reported model, else the exact requested model (recorded as an assumption) | the requested model (Codex reports none; recorded as an assumption) |

The cache split and the modifiers come from the final envelope, in memory. They are frozen only in the pricing
snapshot's `quantities` and are never added to the usage record.

### Claude: whole tree versus top level

Top-level `usage` counts only the top-level agent loop, while `modelUsage` covers the whole agent tree, per model.
The two are never summed. The cost scope is chosen as follows:

- **`invocation_wide_reconciled`:** a single reported model whose breakdown equals `usage`. The top-level figures,
  including the cache split, are priced.
- **`agent_tree_by_model`:** any other breakdown. Every model is priced at its own rate, and helper-model tokens are
  never attributed to the primary model. `modelUsage` has no cache TTL split, so a nonzero cache write stays
  unpriced. A breakdown smaller than `usage` in any category is `model_breakdown_below_usage`, and nothing is priced.
- **No breakdown:** the top level is priced under its own scope (`top_level_agent_loop_unverified`), and the
  assessment is `partial`.

## Money

- **Arithmetic:** `Decimal` in a 60-digit context with the `Inexact` trap set, so any inexact operation raises
  rather than rounds. `amount = tokens × rate / 1,000,000` is exact.
- **Storage:** amounts are stored and aggregated unrounded, as plain decimal strings. JSON never contains a float.
- **Display:** rounded half-even to 4 places. A positive amount below $0.0001 shows as `<$0.0001`.
- **Currency:** USD only, with no conversion.

## Coverage

A line is `complete` only when everything is priced at an invocation-wide scope (`invocation_wide_reconciled` or
`agent_tree_by_model`). Codex `exec_thread_total_unverified` is always `partial`.

`aggregate` reports:

- `estimated_reported_usage_cost`: the known, unrounded sum, or `null`;
- `provider_invocations`, `with_usage`, `priced`, `complete`, `partial`, `unavailable`, `price_unavailable`,
  `usage_unavailable`, `scope_incomplete` and `not_applicable` (command verifiers);
- reason counts;
- `invocation_wide_proven`: true only when every provider invocation is complete;
- the same counts per run, state, agent, provider, model and invocation.

The text output labels a partial sum "known subtotal; not a proven total".

The read path is read-only: no catalog load, network, Git, backfill or writes.
