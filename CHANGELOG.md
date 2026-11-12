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
