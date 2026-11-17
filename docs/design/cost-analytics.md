# Historical cost analytics (ECONOMICS-003, #2)

```
switchyard costs summary (--works-root DIR | --work WORK ...) [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                         [--group-by day|week|month|work|state|agent|provider|model|status]
                         [--provider P] [--model M] [--agent A] [--state S] [--status STATUS] [--last N] [--json]
switchyard.analytics.report(works_root=None, works=(), ...)
```

`costs summary` aggregates the **frozen cost assessments** of [ECONOMICS-004](economics.md) across several works.
It reports the known estimated list-price spend of provider-reported usage. It is never billed spend
(`billed_cost` is always unavailable), and it never presents an incomplete subtotal as total spend.

It is strictly read-only. It takes no lock, runs no recovery or backfill, loads no pricing catalog, reprices
nothing and writes nothing (no index, cache or analytics store). It never launches a provider and never contacts
Git, GitHub or the network. The CLI response is its only output.

## Scope and discovery

The caller must declare the scope. Nothing outside it is scanned: not repositories, the home directory or any
global application data.

- **`--works-root DIR`:** a containing directory. Only its **immediate** child directories are considered; there is
  no recursion. DIR must not itself be a work root (use `--work` for that).
- **`--work WORK`:** an explicit work root (repeatable). It can be combined with `--works-root`. The same directory
  named twice is read once.

A directory is a work root under the work-location contract: it holds a regular `recovery/manifest.jsonl` (the
recovery store), or, for works older than the recovery store, a regular `events.jsonl`. Neither may be reached
through a symlink.

- Symlinked children are skipped and counted (`scope.symlinks_skipped`), never followed.
- Other entries are skipped and counted (`scope.non_work_entries`). That covers files, QA lifecycle workspaces
  and unrelated directories.
- Works are read in sorted name order, then the explicit `--work` order.

**Work identity** is durable, never the directory name: the delivery-branch suffix recorded in the recovery
baseline (`trace.work_identity`, also used by `work trace` and every cost record). A work without a readable
baseline reports `work_identity_unavailable`, and its amounts are excluded.

## Source of every amount

The only monetary source is `<work>/economics/costs/<inv>.json`. Each record is checked before it counts:

| Check | Issue code when it fails |
|---|---|
| Bounded regular file (32 KiB), valid JSON without floats, `invocation_id` equal to its file name | `malformed_cost_record` |
| `schema_version` is the supported cost-record schema (1) | `unsupported_cost_schema` |
| `work_id` equals the work's durable identity | `cost_work_identity_mismatch` |
| `cost_id` equals its deterministic identity | `cost_identity_mismatch` |
| Each line `amount = tokens × rate / 1,000,000` exactly; the lines sum to `estimated_amount`; the status agrees with the lines | `cost_amount_inconsistent`, `cost_status_inconsistent` |
| The referenced pricing snapshot exists, verifies against its `snapshot_id`, and belongs to this work and invocation | `pricing_snapshot_missing`, `pricing_snapshot_altered_or_mismatched` |
| Every line's rate is the frozen rate in that snapshot | `cost_rate_not_in_snapshot` |

Verification recomputes only with the frozen rates; it is never repricing. A record that fails is listed with
status `invalid`, its amount is excluded, and the report cannot be complete. The snapshot's catalog origin,
revision and source are kept for drill-down: `pricing_revisions` lists each frozen catalog origin, revision and
source the counted amounts used, with invocation counts.

Records without a canonical invocation are reported (`cost_record_without_canonical_invocation`) and never counted.

## Unit of accounting

The unit is one canonical invocation: an `invocation_started` event in the work's `events.jsonl`, identified by
`invocation_id` (`run:<uuid>:invocation:<n>`). Each invocation is counted at most once.

- **Retries and failures:** a provider retry or a failed invocation is its own invocation and is counted, because it
  may have consumed money.
- **Recovery:** an assessment written by recovery (`observed_by: recovery_backfill`) is the same invocation's single
  record, so it is not counted twice.
- **Adoption and transplant:** a derived work starts a fresh history. Reused products have no producer invocation in
  the new work, so they add nothing. Continuity savings belong to ECONOMICS-006.
- **Conflicts:** two directories claiming one work identity (`duplicate_work_identity`), or one invocation in
  several works (`conflicting_invocation_identity`), are never resolved by choosing one. Every copy becomes
  `conflicting`, its amount is excluded, and the issue is reported. Command verifiers stay `not_applicable`.

An invocation without a cost record is `unavailable`, with one of these reasons:

- `not_applicable`: its diagnostic shows a command verifier;
- `not_recorded`: the work records costs, but none was written;
- `historical_pricing_not_captured`: the work predates cost recording.

Unknown is never zero.

## Time attribution

Every invocation is dated by the UTC timestamp of its own end event in the canonical log: `invocation_finished`,
or the `run_error` that ended it. File modification times, export times, current work status and the time the
report runs are never used.

| Grouping | Key |
|---|---|
| `day` | UTC calendar date, `YYYY-MM-DD` |
| `week` | ISO week, Monday start, ISO week-year: `YYYY-Www` (2027-01-01 is `2026-W53`) |
| `month` | UTC calendar month, `YYYY-MM` |

`--from` and `--to` are both inclusive calendar dates. They are normalized to the half-open UTC interval
`[from 00:00:00Z, to + 1 day 00:00:00Z)`, which the JSON reports as `period.interval`.

An invocation with no proven end timestamp (`date_unknown`) is handled as follows:

- it counts in the all-time subtotal;
- it is in no period group (time groupings report it as `reconciliation.undated_known_sum`);
- it is excluded from any dated report, and a dated report that excludes an undated provider invocation is never
  complete, because that invocation may belong to the period.

## Dimensions

`--group-by` takes exactly one dimension (default `state`).

- **`work`:** the durable work id, with its directory, current recorded status, delivery category, provider
  invocation count, known subtotal and coverage.
- **`state`, `agent`, `provider`:** the values recorded with each invocation (the cost record, else its diagnostic).
  They are never merged or rewritten.
- **`model`:** each frozen charge line goes to its own model group, so helper-model spend never goes to the primary
  model, and no line appears in two groups.
  - An invocation appears in every model group its lines or unpriced items name, so invocation counts are not
    additive across model groups, but amounts are.
  - An invocation that names no model is counted in `unallocated`.
- **`status`:** the current recorded lifecycle status (see below).

### Ordering and `--last`

- Time groups are chronological.
- Other groups are ordered by known subtotal, descending; unknown subtotals come last, and ties break on the key.
- With `--last N`, time groupings keep the N most recent periods. Other groupings keep the top N, labelled
  `Top N <dimension>s by known estimated spend` (`ranking` in JSON). This ranks known estimated spend only, never
  billed or fully observed cost.

### Filters

`--provider`, `--agent`, `--state` (logical or runtime), `--status` (current work status) and `--model` (charge
lines attributed to that model) narrow the counted invocations. There is no query language.

## Delivery outcome versus invocation outcome

These are two separate classifications.

**Current delivery status.** This is the live, read-only `observed_status` of each work (`work logs` uses the same
reader; it never reconciles). It is mapped to:

- `accepted`: `ACCEPTED`;
- `awaiting_manual_qa`: `READY_FOR_MANUAL_QA` without a QA decision;
- `waiting_for_human`;
- `interrupted`;
- `rolled_back`;
- `active`: an operation in progress, `RUNNING` or `REOPENED`;
- `other`: any other recorded status.

It can change later. An interrupted work may resume and be accepted, and its earlier invocations keep their dates
and amounts.

**Invocation outcome.** This comes from each invocation's recorded diagnostic: `succeeded`, `failed` or `unknown`
(no diagnostic). Failed invocations are split into `infrastructure_failures` (not charged as a semantic attempt)
and `semantic_failures`, using the recorded `semantic_invocation`. A failed invocation does not mean a failed
delivery.

## Coverage

Coverage reuses the ECONOMICS-004 statuses (`complete`, `partial`, `unavailable`, `not_applicable`) and adds
`invalid` and `conflicting`. The report counts:

- provider invocations: priced, completely priced and partial;
- unavailable, usage unavailable, price unavailable, pricing snapshot missing (`pricing_not_captured`,
  `historical_pricing_not_captured`) and not recorded;
- invalid and conflicting;
- provider unknown and date unknown;
- command verifiers (`not_applicable`);
- reason counts.

`coverage.complete` is true only when all of the following hold:

- no issue is reported in the scope (damaged or conflicting records, unreadable logs, limits);
- every provider invocation in scope is `complete`;
- for a dated report, no undated provider invocation is excluded.

When coverage is complete, the subtotal is real spend coverage, and an empty or command-only scope is `0`.
Otherwise:

- `known_estimated_subtotal` is the known part, or `null` when nothing is priced;
- the text reads "Known estimated subtotal", "Complete spend coverage: no" and "Known subtotal is not proven total
  spend";
- it never says "Total cost".

## Money

Arithmetic is `Decimal` with the `Inexact` trap (the ECONOMICS-004 context).

- **Summing:** unrounded record amounts (or charge-line amounts for `model`) are summed, never both.
- **Format:** every amount is an exact decimal string; JSON contains no floats.
- **Display:** text rounds half-even to 4 places (`economics.display`). The header also prints the exact subtotal.
- **Reconciliation:** each counted amount is in exactly one group, so the group subtotals plus, for time
  groupings, the undated sum equal the subtotal. `reconciliation.reconciles_with_subtotal` states it. It is always
  checked over all groups, so a `--last` view never reads as an accounting failure. The sum of per-work subtotals
  equals the global subtotal.
- **`--last` views:** `known_estimated_subtotal` stays the full-scope subtotal. `reconciliation` separates
  `groups_known_sum` (all groups) from `displayed_groups_known_sum` and `omitted_groups_known_sum`, with
  `groups_total`, `groups_displayed` and `groups_omitted`.

## JSON contract (report schema 1)

The report schema is independent of every record schema. Top-level keys:

- `schema_version` (1), `report` (`switchyard_cost_summary`), `kind` (`estimated_list_price_of_reported_usage`),
  `currency` (`USD`);
- `period`: `{from, to, timezone: UTC, boundary: from_inclusive_to_inclusive_calendar_date, interval: {start,
  end_exclusive}, attribution: invocation_end_event_utc, all_time}`;
- `scope`: the discovery facts and limits;
- `filters`, `group_by`, `last`, `ranking`;
- `known_estimated_subtotal` (a decimal string or `null`) and `billed_cost` (always `null`);
- `coverage`: `{complete, provider_invocations, priced, completely_priced, partial, unavailable, invalid,
  conflicting, usage_unavailable, price_unavailable, pricing_not_captured, not_recorded, provider_unknown,
  not_applicable, reasons, works_observed, works_with_issues, date_unknown}`;
- `date_unknown`: `{provider_invocations, known_estimated_subtotal, included_in_subtotal}`;
- `groups`: `[{key, known_estimated_subtotal, complete_coverage, ...the coverage counts}]`, plus the work fields for
  `work`;
- `reconciliation`: `{groups_known_sum, displayed_groups_known_sum, omitted_groups_known_sum, groups_total,
  groups_displayed, groups_omitted, undated_known_sum, reconciles_with_subtotal}`;
- `delivery_outcomes`, `delivery_outcome_basis` (`current_recorded_status_live_classification`),
  `invocation_outcomes` and `invocation_outcome_basis`;
- `pricing_revisions`: `[{catalog_origin, catalog_revision, source, invocations}]`;
- `issues`: `[{work, work_id, code, invocation_id}]`, plus `issues_total` and `issues_truncated`.

The output is deterministic: keys are sorted and groups follow the ordering rules above.

## Bounds

| Bound | Limit | When it is reached |
|---|---|---|
| Works | 1000 | `work_limit_reached`; `scope.works_not_read` counts the rest |
| Invocations | 100,000 in total | `invocation_limit_reached` |
| `events.jsonl` per work | 64 MiB | `events_exceed_bound` |
| Cost record or pricing snapshot | 32 KiB | `malformed_cost_record` or `pricing_snapshot_missing`: excluded |
| Diagnostic | 32 KiB | invocation outcome `unknown` |
| Issues retained | 200 | `issues_total` and `issues_truncated` |

Any limit leaves coverage incomplete; no work is silently skipped. Only these files are read per work:

- the recovery baseline and operation records;
- `events.jsonl`;
- per invocation: one cost record, its pricing snapshot and its diagnostic.

No provider payloads, sources or artifacts are read. Aggregation is in memory.

## Security

The report holds internal identifiers: work ids, directory names, invocation ids, states, agents and models. It
never holds prompts, provider output, source code, credentials, environment values or remote URLs. It is **not** an
anonymized debug bundle and is not asserted to be safe to share publicly.

## Legacy and damaged works

The report states what is provable:

- **Valid frozen records:** counted.
- **Usage without pricing, or a work from before 2.7.0:** `historical_pricing_not_captured`.
- **Before token accounting, or a trace without diagnostics:** unavailable; provider unknown when no record names it.
- **Adopted from an older schema:** read as recorded.
- **A future cost schema:** `unsupported_cost_schema`.
- **A damaged work:** it does not stop the others, but its affected amounts stay excluded and visible.

Nothing is migrated, repriced or rewritten.
