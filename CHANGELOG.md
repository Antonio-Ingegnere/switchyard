# Changelog

## Versioning policy

`pyproject.toml` holds the package version (`switchyard-runtime`), and it is the only source.
`switchyard.versioning.runtime_version()` reads it and new works record it.

- **Every slice bumps the version.** Each completed implementation slice increments the package version and adds an
  entry here, listing only behaviour that is actually implemented.
- **PATCH** is the minimum: fixes and internal changes with no new public surface.
- **MINOR** is for additive public functionality: new CLI commands or options, public API, or new durable, observable
  data. It resets PATCH.
- **MAJOR** is for breaking changes: removed or changed CLI or API behaviour, or incompatible formats. It resets
  MINOR and PATCH.
- **Schema versions are independent.** This covers the work schema (`switchyard.versioning.CURRENT_WORK_SCHEMA`) and
  record schemas such as the trace and diagnostic `schema_version`. Each changes only when its own format changes,
  never because the package version changed.

## 2.8.0

### Added: historical cost analytics (ECONOMICS-003, #2)

`switchyard costs summary (--works-root DIR | --work WORK ...) [--from] [--to] [--group-by
day|week|month|work|state|agent|provider|model|status] [--provider] [--model] [--agent] [--state] [--status] [--last N]
[--json]` aggregates known estimated spend across works. It reads only the frozen ECONOMICS-004 cost assessments.
See `docs/design/cost-analytics.md`.

- **Scope.** The caller declares it: the immediate child work roots of `--works-root` (no recursion; symlinks and
  non-work entries are skipped and counted) and/or explicit `--work` roots. Works are identified by the durable work
  identity recorded in the baseline, never by directory name.
- **Integrity.** Every cost record is validated before it counts: bounded size, schema, identity, exact line
  arithmetic, and its verified pricing snapshot and frozen rates. Damaged, unsupported-schema, mismatched and
  conflicting records are reported and their amounts excluded. A work claimed twice, or an invocation present in
  several works, is never resolved by choosing one copy.
- **Unit of accounting.** One canonical invocation, counted at most once. Retries and failed invocations count;
  recovery-written assessments are not counted twice; adopted products add nothing.
- **Time.** Each invocation is dated by its own end event in `events.jsonl` (UTC). Grouping is by day, ISO week
  (Monday start, ISO week-year) or month. `--from`/`--to` are inclusive dates over a half-open UTC interval. An
  undated invocation counts all-time, in no period, and leaves a dated report incomplete.
- **Dimensions.** One per request. Model attribution follows the frozen charge lines, so helper-model spend stays
  with its own model, and an invocation naming no model goes to `unallocated`. Works are ranked by known estimated
  spend with `--last`.
- **Lifecycle.** The current recorded delivery status (a live, read-only classification) is reported separately from
  each invocation's recorded outcome (succeeded, failed — split into infrastructure and semantic — or unknown).
- **Coverage.** Complete only when every provider invocation in scope is completely priced and no issue or limit
  applies. Otherwise the text shows a "Known estimated subtotal" with "Complete spend coverage: no"; unknown is never
  zero.
- **Money.** Exact `Decimal` sums of unrounded amounts, decimal strings in JSON (report schema 1, independent of
  record schemas), display rounded half-even to 4 places. Group subtotals reconcile with the subtotal.
- **Read-only and bounded.** No lock, recovery, backfill, catalog load, repricing, provider, Git or network, and no
  persistent index. Works, invocations, log size, record size and retained issues are bounded; reaching a bound is
  reported.
- No work, trace, usage, pricing or cost record schema changed.

## 2.7.0

### Added: pricing registry and cost attribution (ECONOMICS-004, #2)

`switchyard work costs WORK [--json] [--run] [--state] [--agent] [--provider] [--model] [--invocation]` reports the
estimated list-price cost of each invocation's provider-reported usage. It never reports a billed or invoiced amount
(`billed_cost` is unavailable).

- **Registry.** A checked-in, versioned catalog (`switchyard/pricing_catalog.json`, revision `2026-11-13.1`) holds
  decimal-string rates in USD per million tokens. Every entry has a source URL, a verification date, an
  applicability window and a basis.
  - Built-in rates were verified on 2026-11-13 against the official Anthropic and OpenAI pricing pages.
  - An optional explicit user catalog (`SWITCHYARD_PRICING_CATALOG`) replaces built-in entries per model.
  - Unknown models, aliases and per-request context-tiered models (Claude Haiku 5.5, gpt-5.4, gpt-5.5, GPT-6) are
    never priced.
  - There is no remote pricing service.
- **Frozen snapshots.** Each ended invocation gets an immutable pricing snapshot (`economics/pricing/`) holding the
  exact rates, provenance, registry hashes and selection basis, plus the frozen chargeable quantities (including
  Claude's 5m/1h cache-write split), content-addressed by `snapshot_id`. It also gets an
  immutable cost assessment (`economics/costs/`) that references the snapshot. A later catalog change never alters an
  existing assessment.
- **Recovery.** Recovery assesses the snapshot's frozen quantities at its frozen rates, the same inputs as the
  uninterrupted assessment, so a crash between the two publications never changes the cost. It never reads the
  current catalog. Without a
  contemporaneous snapshot, the cost is `pricing_not_captured`, never today's price. Works created before 2.7.0 read
  as `historical_pricing_not_captured`.
- **Claude.** Input, cache read, 5m and 1h cache writes, and output are priced separately.
  - Cache writes are priced only with the reported `cache_creation` TTL split, otherwise left unpriced.
  - A reconciled single model is priced from the top level.
  - Any other breakdown is priced per model across the whole agent tree, at each model's own rate. It is never
    added to top-level usage.
  - Top-level usage without a breakdown stays `partial`.
- **Codex.** Uncached input is input minus cached input, and only when the counters are consistent. Reasoning is
  priced once, inside output.
- **Money.** Exact `Decimal` arithmetic with inexact operations trapped. Amounts are unrounded decimal strings in
  records and JSON; display rounds half-even to 4 places.
- **Coverage.** Each record is `complete`, `partial`, `unavailable` or `not_applicable`, with reasons and assumptions.
  Totals are a known subtotal, with counts of priced, partial, price-unavailable, usage-unavailable and
  scope-incomplete invocations. `invocation_wide_proven` is true only when everything is complete.
- **Isolation.** Writes are failure-isolated from delivery. Reads are read-only (no catalog load, network or
  backfill). Usage records are unchanged.

Pricing snapshot and cost assessment records start at schema 1. Work, trace, diagnostic and usage schemas are
unchanged.

## 2.6.1

### Added: Codex token usage (ECONOMICS-001B, #2)

`switchyard work usage` now reports Codex usage. No new command or option.

- **Invocation.** Codex runs as `codex exec --json`; every other flag is unchanged. The StageResult still comes only
  from `--output-last-message`. The JSONL stdout is usage telemetry and is never parsed as a result. A Codex CLI
  without `--json` rejects the flag and fails through the existing provider-exit handling, with no fallback launch.
- **Bounded capture.** A streaming parser handles arbitrary chunk boundaries and keeps only the validated counters
  of each `turn.completed` event. At most 64 KiB of one line is buffered; longer lines are skipped while the stream
  keeps draining. No event, message, command, tool output, file change or thread id is kept or persisted.
- **Semantics.** `turn.completed.usage` is the exec thread's cumulative total. Several snapshots are never summed:
  the latest wins, and an earlier one that exceeds it makes the usage invalid. Input includes cached input. Reasoning
  output is a subset of output, recorded separately in `output_detail.reasoning_output` and never added. Cache
  writes are kept only when reported.
- **No invented zeros.** An all-zero `turn.completed` is the CLI's default when no token update arrived, so it is
  `unavailable` (`unverified_zero_usage`). These are also `unavailable`, never zero: a failed turn without usage
  (`turn_failed_without_usage`), a run with no events (`no_jsonl_events`), and a parser fault
  (`usage_capture_failed`), which never changes the delivery.
- **Scope.** Codex records are labelled `exec_thread_total_unverified` and are `partial`, because the coverage of
  spawned agent threads is not documented.
- **Mixed providers.** When Claude and Codex both report, the combined input total is withheld
  (`withheld: mixed_input_semantics`) because the two input conventions differ. Input is shown per provider, and the
  derived "input incl. cache" is still summed.

Usage record schema stays 1. `output_detail` is a new optional field, and records written by 2.6.0 read it as
unknown. No work, trace or diagnostic schema change.

## 2.6.0

### Added: provider token usage (ECONOMICS-001, #2)

`switchyard work usage WORK [--json] [--run] [--state] [--agent] [--provider] [--model] [--invocation]` reports the
provider-reported token usage of every invocation.

- **Coverage.** Output shows per-invocation counts, totals of known reported values only, groups by run, state,
  agent, provider, model and invocation, and coverage: reported, partial, unavailable, or not applicable to
  verifiers.
- **No invented numbers.** Unknown counts are `—` or `unavailable`, never zero. No costs are estimated or shown.
- **Claude.** Usage is read once from the final cumulative result envelope: input, output, cache read and cache
  write. Structured-output repair turns are not counted twice. `modelUsage` keeps the reported model apart from the
  configured one, and a multi-model breakdown is never split by guessing.
- **Claude coverage scope.** Top-level `usage` covers only the top-level agent loop; subagent and auxiliary model
  calls are excluded.
  - Each record carries a `coverage_scope`. It reads invocation-wide only when the whole-tree `modelUsage` breakdown
    reconciles exactly with `usage`.
  - Totals are labelled with their scope, and the breakdown is reported separately, never added.
  - A zeroed crash result is not trusted.
- **Codex.** The adapter's `codex exec` mode reports no usage, so its invocations are recorded as unavailable rather
  than estimated. A normalizer for the documented JSON usage events exists but is not yet wired.
- **Cache semantics.** Input semantics differ by provider: Claude input excludes cache tokens, Codex input includes
  them. Totals are kept per category, with a separately derived "input incl. cache".
- **Records.** Each invocation gets one immutable record under `<work>/observability/usage/` (usage record schema 1).
  Retries keep their own records.
- **Failures and recovery.** Failures are isolated from delivery. Recovery backfill records `unavailable`
  (`not_captured`) and never reruns a provider.
- **Legacy works.** They report "not recorded by this runtime version".

No work, trace or diagnostic schema change. The debug bundle does not include usage yet.

## 2.5.0

### Added: debug bundle (#1)

`switchyard work debug-bundle WORK --output PATH [--json]` writes a sanitized, bounded ZIP of one work's observability
evidence, so the work can be investigated without access to its directory.

- **Contents:** `manifest.json`, `README.md`, `runtime.json`, `status.json`, `trace.jsonl` and `diagnostics.jsonl`.
  - Runtime, workflow, configuration and Git fingerprints.
  - Lifecycle status.
  - Trace events in sequence order.
  - Per-invocation diagnostics: classification, exit code, timeout, duration, validation path and category,
    verification result and artifact digests.
- **Safe by construction.** Every exported field is allowlisted and type-checked. These are never exported: prompts,
  provider payloads, stdout/stderr, source, environment, credentials, configuration contents, remote URLs, absolute
  paths, verification output, human answers and free-text diagnostics. Values that fail a check are listed under
  `excluded_for_safety`. There is no option to include more.
- **Pseudonymized names.** User-chosen names (work, flow, state, agent, model, artifact, outcome, branch, remote,
  executable and so on) appear as consistent `<category>-<hex>` aliases. The key is derived from data that never
  enters the bundle, and the mapping is never exported. Built-in providers, models and Switchyard vocabulary are kept.
- **Honest completeness.** The manifest reports trace, diagnostics and history completeness. It counts and lists
  corrupt, unsupported, missing, gapped and unfinished evidence, and never claims a complete history it cannot prove.
- **Bounded and deterministic.**
  - Explicit limits: 10,000 events, 5,000 diagnostics, 32 KiB per record, 16 MiB uncompressed and 16 MiB
    compressed. Exceeding one fails with an error; nothing is truncated.
  - The same source data gives byte-identical archives.
- **Read-only and atomic.**
  - The export takes no lock, runs no recovery, Git, network or provider.
  - Output must lie outside the work. It is published by exclusive link, so it never overwrites an existing file,
    and no partial file is left on failure.

No work, trace or diagnostic schema change. The bundle itself has `bundle_schema_version` 1.

## 2.4.0

Everything added since 2.3.0. Works created by 2.3.0 (work schema 2) remain readable and adoptable.

### Added: isolated delivery (#3, #4)

- **Isolated deliveries.** New deliveries run on their own `switchyard/<work>` branch and worktree, created at the
  source checkout's clean HEAD. The caller's checkout is never modified.
- **Acceptance.** `switchyard work accept` commits the accepted candidate durably on the delivery branch.
- **Suspend and resume.** `switchyard work suspend WORK [reason]` shelves the worktree's uncommitted state and removes
  the worktree. `switchyard work resume` restores it exactly, then continues.

### Added: manual QA loop (#6, #7)

- **Rejection without restarting.** `switchyard work qa-fix WORK "<feedback>"` rejects the current candidate and
  continues the same work through its declared corrective route. This repeats over durable QA rounds and is
  crash-safe.
- **Structured findings.** Give findings with `--finding TEXT` (repeatable) and evidence files with `--attach PATH`.
- **Evidence delivery.** Evidence is stored durably and delivered to the corrective agent: text inline, images only
  to models that accept image input. Otherwise it is refused before launch.

### Added: continuity (#5, #8)

- **Adopt.** `switchyard work adopt OLD NEW [--source]` reuses an old work's implementation in a new isolated work and
  re-runs only the verifier. The implementation can come from an accepted commit, a suspended shelf, or an undecided
  QA candidate. Adoption works on the same base or a changed base, and from 2.3.0 works.
- **Transplant.** `switchyard work transplant OLD NEW [--source]` explicitly moves a delivery onto a different base,
  the source checkout's HEAD.
- **Old work untouched.** Both commands only read the old work. Reuse decisions are explicit and recorded.

### Added: integration and publication (#9)

- **Readiness.** `switchyard work integration-status WORK [--target] [--json]` assesses an accepted delivery against
  its target: ready, ready with drift, already integrated, or blocked (with reasons).
- **Local integration.** `switchyard work integrate WORK [--target] [--json]` integrates into the local target
  branch atomically: a fast-forward, or a merge commit when the target moved without conflicts. It works on a
  checked-out target, and never overwrites local changes.
- **Push.** `switchyard work push WORK --remote R [--target] [--json]` publishes the local integration by
  compare-and-swap (`--force-with-lease`, never plain `--force`). Switchyard stores no credentials and no URLs with
  embedded secrets.
- **Pull requests.** `switchyard work pr-create WORK --remote R [--target] [--title]` opens or reconciles a GitHub
  pull request through `gh`. Retrying after a crash finds the existing pull request rather than creating a duplicate.
  `switchyard work pr-status WORK` reports its state (OPEN, CLOSED or MERGED), mergeability and head drift.

### Added: observability (#1)

- **Durable trace.** `<work>/observability/events/` holds immutable, sequence-ordered events projected from the
  authoritative `events.jsonl`, correlated by work, run, visit and invocation. They cover routing, budgets, outcomes,
  transitions, human input, interruptions and provenance. Trace failures never affect execution, and missing events
  are backfilled from durable records only.
  - `switchyard work trace WORK [--json] [--run] [--state] [--invocation] [--type]`
  - `work status` gains an `observability` summary.
- **Provider diagnostics.** `<work>/observability/diagnostics/` holds one bounded record per invocation: process
  metadata, the existing failure classification, structured-output validation and repair facts, and verifier
  evidence references. Raw provider stdout and stderr, prompts and payloads are never stored.
  - `switchyard work diagnostics WORK [--invocation ID|I<n>] [--json]`
- **Logs and watch.**
  - `switchyard work logs WORK [--run] [--state] [--invocation] [--last N] [--json]` shows a compact timeline in
    trace order, with diagnostic references for failed invocations.
  - `switchyard work watch WORK [--interval S] [--last N] [--json]` follows new events by sequence cursor (NDJSON
    with `--json`) and exits with the next action on a terminal or waiting state.
  - Both are read-only.

### Schemas

- **Work schema 3** is new: isolated delivery provenance. New works record `work_schema_version` and
  `runtime_version`.
- **Trace and diagnostic records** are new, at schema version 1.
