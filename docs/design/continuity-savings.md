# Continuity savings (ECONOMICS-006, #2)

```
switchyard work savings WORK [--json] [--source-details] [--invocations]
switchyard.savings.report(work_root)
```

For a work derived by `work adopt` or `work transplant` ([continuity](delivery-recovery.md#continuity-adopt-prior-work-5-slice-1)),
`work savings` explains the economics of reuse. It keeps three amounts strictly apart:

| | Meaning | Whose money |
|---|---|---|
| **A. Historical source expenditure** | Frozen cost of the source executions behind the reused products ([economics](economics.md)) | The original work's, never the derived work's |
| **B. Actual new expenditure** | The derived work's own frozen cost records: its verifier, corrective QA rounds, retries | The derived work's |
| **C. Estimated avoided regeneration** | `historical_execution_proxy`: the known historical cost of the invocations proven to have produced the reused products | Nobody's: a counterfactual comparison |

C is never billed or proven savings (`billed_savings: null`, `actual_savings: not_established`). Regeneration
might use another provider, model, prompts, tokens, retries or prices. A changed-base transplant is labelled
separately (`transplanted_to_a_different_base`): a deterministic transplant made no provider call, but that does
not show what regenerating the product on the new base would cost.

For a work that was never derived, the report says "No continuity derivation recorded." Avoided regeneration is
`not_applicable` there, not zero.

## Provenance and attribution

The reuse facts come from the derived work's own durable adoption record in its baseline: intent, source work,
source baseline digest, base(s), implementation identity and digest, transplant result, and the continuity
decisions with their artifact identities. They stay available when the source work is gone.

For each `REUSED` decision, the report traces the product back to the work whose invocations produced it.

- **Verification before reading.** Each recorded source hop (`from_work_root`) is checked before any of its
  economics is read:
  - it is a real directory reached without a symlink;
  - its latest baseline digest equals the recorded `from_baseline_sha256`;
  - its work identity equals the recorded source;
  - every reused artifact identity is in its manifest, with the same bytes.

  The first failure stops the trace for that decision, with a bounded reason. Another work at the same path or with
  the same name is never accepted.
- **The producing invocation.** A source invocation is *attributable* only when its live trace record
  (`invocation_finished.artifact_refs`, captured while it ran) names the exact reused artifact (name, version,
  sha256).
  - Other invocations of the producing stage (failed attempts, earlier QA rounds, superseded products) are
    *associated but unattributed*. They are reported, never put into the proxy.
  - An invocation with no live refs (backfilled or untraced) proves nothing:
    `source_invocation_attribution_unavailable`.
- **Transitive derivation (W1 → W2 → W3).**
  - If the hop holds the artifact only because it reused it itself (its own adoption record has the same artifact
    sha256), the trace continues to that hop's recorded source and verifies it in the same way.
  - For the implementation, the digest the next work adopted must equal the digest this hop adopted (the transplant
    result when the base changed). Otherwise the hop changed the product: `implementation_identity_mismatch`.
  - So W3's implementation is attributed to W1's producing invocation, never to IMPLEMENT spending W2 did not have.
  - Hops are bounded (16), cycles are detected by baseline digest, and at most 32 works are read per report.
- **Durable inputs.** `REQUEST` and `SOURCE_RESOLUTION` are durable inputs with no model execution (`not_applicable`).
- **Other decisions.** `RERUN` is actual execution in the derived work, under B. `INVALIDATED` is never avoided
  execution.

## Deduplication and additivity

- **Within one report:** attributable and associated invocations are keyed by (producing work baseline,
  `invocation_id`). An invocation behind several artifacts or decisions counts once, and an attributable invocation
  is never also counted as associated.
- **Across derived works:** W2 and W4 can both reuse W1, and their proxies can name the same historical
  invocations. Each proxy is a separate counterfactual comparison. They are **not additive**
  (`not_additive_across_derived_works`), and this slice has no organization-wide avoided-cost total.

## Money and tokens

Amounts come only from frozen cost records, validated by the ECONOMICS-003 reader:

- schema and identity;
- exact line arithmetic;
- a verified pricing snapshot, using frozen rates only.

Nothing is repriced, backfilled or zero-filled. Sums are exact `Decimal` strings, and text display rounds half-even
to 4 places. Coverage uses the existing terms (`complete`, `partial`, `unavailable`):

- a partially priced source invocation is never complete;
- the proxy is `partial` whenever any reused product lacks proven attribution, the producing stage has
  unattributed invocations, or a source issue applies.

Tokens are the provider-reported usage of the attributable invocations, **per provider** (`work usage` semantics):

- Codex input includes cached input;
- Claude top-level usage may exclude subagents, and the per-model breakdown is shown separately;
- reasoning output is a subset of output;
- unknown categories stay unknown.

They are the basis of the proxy, not tokens that a regeneration would use.

## Evidence flags

`evidence.flags` keeps the facts apart:

- **Reuse:** `reuse_proven` or `reuse_incomplete`, and `reuse_provenance_incomplete`.
- **Source evidence:** `source_economics_unavailable`, `source_usage_unavailable`, `source_cost_unavailable`,
  `source_invocation_attribution_uncertain` or `_unavailable`, and `source_pricing_partial`.
- **Derived work:** `derived_work_cost_partial` or `_unavailable`.
- **Result:** `historical_proxy_available` or `_unavailable`.

A work can have provably reused a product while its economic value is unknown.

When the source work is missing, the report shows:

- reuse provenance: available;
- original expenditure: unavailable;
- avoided-cost proxy: unavailable;
- reason: `source_economics_unavailable`.

A blocked (conflicting) transplant creates no derived work, so nothing is claimed.

## JSON contract (report schema 1)

The report schema is independent of every record schema. Top-level keys:

- `schema_version`, `report` (`switchyard_continuity_savings`), `work_id`, `directory`, `currency`, `derived`;
- `derivation`: `{intent, complete, phase, source_work_id, source_baseline_sha256, source_base_sha, target_base_sha,
  base_changed, implementation_source, implementation_identity, source_product_digest, transplant}`;
- `reuse`: `{decisions: [{stage, product, action, reason, artifacts, implementation_digest, provenance: {attribution,
  reason, producer_work_id, producer_directory, hops: [{work_id, directory, verified, reason}], attributable,
  associated}}], reused_stages, rerun_stages, invalidated_stages, reuse_proven}`;
- `historical_source`: `{producers, attributable_to_reused_products, stage_associated, associated_but_unattributed,
  attributable_invocations, unattributed_invocations, tokens, basis: frozen_source_cost_records}`;
- `actual_new_work`: `{known_estimated_cost, coverage, ...counts, by_state, tokens}`;
- `avoided_regeneration`: `{basis: historical_execution_proxy, available, known_estimated_cost, coverage, reasons,
  base_changed, caveats, billed_savings: null, actual_savings: not_established}`;
- `evidence`: `{flags, works_read, work_limit_reached}`;
- `issues` and `issues_total`.

Decisions keep the order of the adoption record. Invocation ids and keys are sorted.

## Read-only guarantees

The report takes no lock and changes nothing in the source or derived work:

- it never mutates adoption or continuity records;
- it never reruns stages;
- it never creates snapshots or loads the current catalog;
- it never runs recovery or backfill;
- it never contacts a provider or Git.

It reads only baselines, operation records, artifact manifests, the reused artifacts' bytes, trace
`invocation_finished` records, and cost, pricing, diagnostic and usage records, all bounded. It exports no prompts,
artifact contents, provider payloads, credentials or human feedback.
