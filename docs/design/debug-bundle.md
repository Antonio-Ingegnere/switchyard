# Debug bundle (OBSERVABILITY #1, slice 4)

```
switchyard work debug-bundle WORK --output PATH [--json]
switchyard.bundle.create_bundle(work_root, output)
```

The bundle is a portable export of proven observability evidence. It is not runtime state: it cannot resume, recover
or replace a work.

## Layout

The entries are fixed, in this order. No entry name is ever derived from source data.

| Entry | Content |
|---|---|
| `manifest.json` | `bundle_schema_version`, exporter `switchyard_version`, `work_id`, completeness, counts, sources, issue counts and the first 50 issues, limits, and SHA-256 and size of every other entry (never itself) |
| `README.md` | Static explanation of files, markers and exclusions |
| `runtime.json` | Exporter version. Baseline: recorded runtime version, work schema, lifecycle contract, flow, configuration SHA-256, baseline record digest. Git: base SHA, delivery and source branch names. Latest integration, push and pull request phases and commit SHAs, or `not_applicable` |
| `status.json` | Status from the pure status reader (`logs.observed_status`), plus the latest operation's state, reason, `resume_safe`, terminal, human request id and accepted commit |
| `trace.jsonl` | Trace events in sequence order, sanitized |
| `diagnostics.jsonl` | Diagnostic records in invocation-id file order, sanitized |

## Allowlist, never raw copies

Each source record is handled in four steps:

1. It is read only if it is a regular, non-symlinked file of at most 64 KiB.
2. It is parsed, and its schema is checked (trace and diagnostic `schema_version` 1).
3. Its identity is checked against its file name: sequence and `event_id`, or invocation id and `diagnostic_id`.
4. Only allowlisted fields are rebuilt, each type-checked.

Field shapes:

- **Identifiers:** `[A-Za-z0-9@][A-Za-z0-9_.:@+=/-]{0,127}`, never containing `..`.
- **Ids:** run, visit and invocation ids by pattern.
- **Digests and SHAs:** 32-hex ids, SHA-256, Git SHA-1 or SHA-256.
- **Others:** ISO-UTC timestamps, integers, numbers, booleans.

Field outcomes:

- **Failed check:** the value is not exported (`null`), and its code-owned path is listed in the record's
  `excluded_for_safety`.
- **Free text and paths:** never exported, and listed under `excluded_for_safety` when the source held a value. This
  covers summaries, error messages, the trace `diagnostic`, provider error messages, stderr excerpt text, artifact
  paths, worktree paths and `run_started` provenance (sanitized once in `runtime.json` instead).
- **Unknown fields:** dropped. They are counted (`unrecognized_fields_dropped`), never named.
- **Oversized lists:** a list over 256 items is excluded, never truncated.
- **Validation errors:** exported only as `{path, category}`. The category is `additional_property`,
  `missing_required`, `type_mismatch` or `other`, and the path is dropped if it fails its check.

These are never exported, and no option exports them:

- prompts, provider responses and envelopes, stdout/stderr;
- source and generated code;
- environment, credentials, configuration contents;
- remote URLs and identities, absolute paths;
- verification output, human answers, QA evidence contents;
- future-schema payloads.

## Pseudonyms

User-controlled names are pseudonymized as `<category>-<12 hex>` across all files, including `manifest.work_id`:

- work id, flow, state, agent, model, artifact, outcome, role, branch and remote;
- verifier executable, tool names;
- validation-path segments outside the StageResult contract (`field-…`).

Composite runtime ids are rebuilt from their aliased parts, so correlation survives:

- runtime states and policy identities: `@v2:<flow>:<state>:<rung>`;
- budget scopes: `@v2-scope:<flow>:<state>:state|rung:<n>`;
- budget metrics: `invocation_scope:<scope>`;
- branches: `refs/heads/switchyard/<work>`.

What is kept and what is re-derived:

- **Kept:** Switchyard's own vocabulary (snake_case codes, event types, statuses, `READY_FOR_MANUAL_QA`), the built-in
  providers (`claude`, `codex`, `command`), the audited built-in models and tools, generated roles (`state-<n>`), the
  capability names, and the reserved `budget_exceeded` outcome.
- **Re-derived:** event and diagnostic ids, because they are hashes over the work id and would confirm a guessed name.
  They are re-derived under the bundle key, so trace-to-diagnostic links still match within the bundle.

The alias is an HMAC of the category and name. The key is derived from data that never enters the bundle: the frozen
configuration text, or for works without a baseline, the canonical history. Consequences:

- Aliases are deterministic for an unchanged snapshot and independent of directory iteration order.
- The original-to-alias mapping is not in the archive and cannot be recomputed by guessing names.
- Works sharing a configuration share aliases.
- With neither key source available, a random key keeps aliases consistent only within that one bundle.
- A collision aborts the export; two identities are never merged.

The CLI still says secrets are "not intentionally included" rather than guaranteeing it: digests such as
`config_sha256` remain fingerprints.

## Markers and completeness

**Markers.**

- **`null`:** the source does not hold the value (not captured or unknown).
- **`excluded_for_safety`:** the source held the value, but it was not exported.
- **`not_applicable`:** a whole source does not apply (for example, no integration yet).
- **`unavailable` / `excluded_for_safety`:** a store is missing, or is a symlink (`manifest.sources`).

**`completeness.trace`.**

- **`complete`:** only when every trace file is valid and supported, sequences are dense from 1, event ids are unique,
  and every canonical `events.jsonl` event is projected.
- **`partial`:** anything else, including an unreadable `events.jsonl` (completeness then cannot be proven).
- **`unavailable`:** no trace store.

**`completeness.diagnostics`.**

- **`complete`:** every traced invocation has a valid record, there are no orphans or invalid records, and the trace
  is complete.
- **`partial`:** anything else.
- **`unavailable`:** no store and no invocations.
- **`not_applicable`:** a complete trace with no invocations.

**`completeness.history`.**

- **`complete`:** a complete trace, no invocation without a proven end (finish or interruption), and an inactive
  status.
- **`partial`:** anything else.

An invocation that only started stays `invocation_without_proven_end`; nothing is invented after it. Issues use
code-owned reasons:

- `corrupt_record`, `unreadable_symlinked_or_oversized_record`, `record_does_not_match_its_name_or_type`,
  `record_does_not_match_its_identity`;
- `duplicate_event_id`, `unsupported_schema_version`, `unexpected_entry`;
- `missing_sequence`, `canonical_event_not_traced`, `canonical_history_unreadable`;
- `missing_diagnostic_for_finished_invocation`, `invocation_without_proven_end`, `diagnostic_without_traced_invocation`.

## Limits

| Limit | Value |
|---|---|
| trace events | 10,000 |
| diagnostic records | 5,000 |
| one exported record | 32 KiB |
| one source record | 64 KiB (larger means an invalid source, reported) |
| uncompressed bundle | 16 MiB |
| compressed bundle | 16 MiB |

Exceeding an export limit fails with a bounded `bundle limit exceeded: …` error. Nothing is truncated. Records are
streamed through temporary files outside the work, not held in memory.

## Publication

- **Output location:** the output must not exist and must lie outside the (resolved) work directory, and its
  directory must exist.
- **Staging:** the ZIP is built as a hidden `.<name>.*.partial` file in the destination directory, then fsynced.
- **Exclusive publication:** it is published with `os.link`, which fails if the destination appeared meanwhile, so the
  destination is never clobbered. The temporary file is always removed.
- **No mutation:** source records are never modified.

## Determinism

- Trace order is by sequence; diagnostics are in sorted file order.
- JSON is canonical: sorted keys, compact.
- Entry order is fixed.
- Every entry is a regular file (mode 0644), deflated, with timestamp 1980-01-01.
- There are no generation timestamps or random ids.

Unchanged source data gives byte-identical archives on the same Python/zlib.

## Read-only

The export takes no controller lock, runs no recovery or backfill, and makes no Git, network or provider calls.
Status comes from the pure reader, never `recovery_status`. The only write is the requested archive.
