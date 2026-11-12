# Provider diagnostics (OBSERVABILITY #1, slice 3A)

```
switchyard work diagnostics <work> [--invocation ID|I<n>] [--json]
switchyard.diagnostics.read_diagnostics(work_root, invocation_id=None)
```

These answer *why* one invocation ended as it did. The runtime trace (`runtime-trace.md`) answers *what* ran. A
diagnostic is observational evidence, never authority. These stay authoritative and unchanged:

- `events.jsonl`;
- the recovery operation records (`interrupted`: reason, `semantic_invocation`, `provider_failure`);
- the artifact manifest.

Nothing in execution reads a diagnostic.

## Record (schema 1)

There is one record per `invocation_id`, using the runtime's own identity.

| Field | Meaning |
|---|---|
| `diagnostic_id` | `sha256(work_id, invocation_id, "diagnostic")[:32]`. Deterministic, with no timestamps. |
| `work_id`, `run_id`, `state_visit_id`, `invocation_id` | The same correlation identities as the trace. |
| `state`, `runtime_state`, `rung`, `agent`, `provider`, `model` | The compiled execution binding. |
| `observed_by` | `runtime` or `recovery_backfill`. |
| `completeness` | One of three values: <br>• `complete`: live, process facts captured. <br>• `partial`: backfilled, or no process metadata exists (for example a stub stage). <br>• `unavailable`: read-time placeholder only, never persisted. |
| `timeout_seconds`, `duration_ms`, `duration_source` | Configured timeout and actual duration. The source is `process`, `canonical_event` or `interrupted_operation`. |
| `result` | `produced_stage_result`, `outcome`, `outcome_category`, `classification`, `reason`, `semantic_invocation`, `resume_safe`, `error`. |
| `process` | `launched`, `exit_code`, `termination_signal`, `terminated_by` (`switchyard_timeout` or `signal`), `timed_out`, `timeout_seconds`, `duration_ms`, `launches`. Null when not observed. |
| `provider_channel` | Documented Claude result-envelope fields only (see Streams). |
| `stderr_excerpt` | Always `{stream, available: false, reason, bytes_seen?}`: stream content is never retained (see Streams). |
| `structured_output` | `schema {name, transport, sha256}`, `valid`, `error`, `errors[]` (`path`, `error`, `allowed_fields`), `errors_truncated`, `repair {mechanism, max_attempts, attempted, succeeded}`, `raw_result_retained: false`. |
| `verification` | Command verifiers only: <br>• `verifier`, `executable` (basename), `argc`, `argv_sha256`; <br>• `exit_code`, `timed_out`, `outcome`; <br>• `outcome_rule` (`declared_exit_code` or `default_outcome`); <br>• `evidence_artifact`, `evidence_refs`. |
| `artifact_refs` | Manifest references written by this invocation (never copies). |
| `summary` | A code-owned explanation of at most 2 KiB. |
| `truncated` | `true` when anything in the record was cut. |

### No second failure taxonomy

`classification` is the existing `recovery._classify` result:

- `timeout`, `rate_limit`, `authentication`, `provider_unavailable`;
- `transport_failure`, `invalid_stage_result`, `provider_exit`.

`reason` is the existing interruption reason, and `semantic_invocation` is the existing charge decision. The diagnostic
only exposes the distinctions the runtime already draws:

- **Semantic outcome.** `produced_stage_result: true` with `outcome_category: semantic_outcome`. A verifier `fail`
  is a declared semantic outcome and is charged.
- **Infrastructure or provider failure.** `produced_stage_result: false` with a classification such as
  `provider_exit` or `timeout`. A launch failure shows `process.launched: false` (`provider_unavailable`). When
  proven, it is voided (`semantic_invocation: false`).
- **Result failure.** `invalid_stage_result`: the provider answered, but no valid StageResult exists. The runtime
  keeps treating it as non-semantic, exactly as before.
- **Post-result failure.** `produced_stage_result: true` together with a `reason`: the runtime rejected the result
  after the adapter returned it, and the invocation stays charged.

Charging and budgets are unchanged.

## Streams

| Channel | Retained | Restriction |
|---|---|---|
| Provider stdout (all providers) | **never** | It is the result channel: generated code, prompts and payloads. |
| Claude result envelope | documented fields only | See below. `structured_output`, `result` prose (unless `is_error`), usage, cost and session ids are never kept. |
| LLM-provider stderr (Claude and Codex) | **never** (`unstructured_output_not_retained`) | Arbitrary text: no sanitizer can prove it free of source, prompts or model output (Codex stderr also carries the session transcript). Only `bytes_seen` is kept. |
| Verifier (command) stdout/stderr | **never** | The declared capture artifact is the evidence; the record references it (`evidence_refs`). |
| Raw invalid structured result | **never** | Only validation paths and code-owned messages are kept; a rejected key name is clipped. |

The Claude envelope fields kept are:

- `subtype`, `terminal_reason`, `api_error_code`, `api_error_status`, `is_error`, `num_turns`;
- `errors[]` messages, each passed through the sanitization gate;
- `permission_denials` tool names.

### Sanitization gate (`diagnostics.sanitize`)

The gate applies only to the documented, structured error messages above, never to a stream. It is a defence in depth,
not proof of safety, which is why unstructured stderr is not persisted at all. It first strips ANSI and control characters. Redaction alone is not trusted. The whole text is rejected, with
reason `unsafe_unstructured_output`, when it contains any of the following:

- a credential-pattern match: `sk-…`, `Bearer …`, `token=`/`password=`/`api_key=`, GitHub/Slack/GitLab tokens, AWS keys;
- URL userinfo;
- an `Authorization`/`Cookie` header;
- a PEM key or certificate;
- three or more `NAME=value` lines (an environment dump);
- an opaque token of 32 or more characters mixing upper case, lower case and digits;
- a prompt section header or a StageResult payload key.

Partially redacted output is never kept. Code-owned text (`summary`, `error`) is clipped and credential-stripped.

## Bounds

| Item | Limit |
|---|---|
| summary / error | 2 KiB |
| validation errors | 20 entries (the parser itself produces at most 8) |
| Claude error messages | 16 × 512 B |
| artifact refs | 20 |
| whole record | 32 KiB |

`execute()` keeps only a stderr byte count, never stream bytes, while it drains both pipes as before. Over the record
limit, trailing errors, messages and refs are dropped. Truncation is always visible (`truncated: true`,
`errors_truncated`).

## Storage and identity

- **Layout:** `<work>/observability/diagnostics/schema.json` (version and limits) plus `<invocation_id with ':' → '_'>.json`.
- **Publication:** each record is written as an fsynced temporary file and published with an exclusive hard link. A
  record is never overwritten. If a record exists, a retried recovery writes nothing.
- **No supplementary records:** records are write-once. A fact learned later is not merged in.
- **Frozen stores untouched:** no frozen catalog or existing store is modified.

## Lifecycle, failure isolation and recovery

- **Capture.** `provider_process.observe_invocation()` is a context variable set around each adapter call in
  `_CheckpointExecutor`. `execute()` and the adapters write observational facts into it. Every update is
  failure-isolated, and execution never reads it.
- **Finished invocation.** The record is written when the canonical `invocation_finished` event is durable.
- **Stopped invocation.** For a `run_error` with an invocation, the record waits for the `interrupted` operation
  record and is written with it.
- **Failure isolation.** All writer entry points swallow their own errors. A failed write means a missing record and
  nothing else: no retry, no charge, no lifecycle, candidate or result change.
- **Backfill.** On the next drive (resume, qa-fix, answer, reopen), missing records are reconstructed only from proof:
  - a finished invocation gives its outcome and canonical duration;
  - an interruption gives reason, classification, exit code, `timed_out` and duration from its operation record.
  
  Process metadata and streams stay null or `not_captured`.
- **Killed controller.** An invocation that only started, because the controller was killed, gets no record. The read
  API lists it as `completeness: unavailable` and invents no exit code or result.

## Trace correlation

`invocation_finished` and `run_interrupted` trace events carry `payload.diagnostic_id` when the diagnostic was already
published at the moment the trace event was written (otherwise `null`). Existing trace records are never rewritten.
The read API always joins on `invocation_id`, so a later backfilled diagnostic is still found.

`followed_by_verification` is computed at read time, not persisted. A verifier is linked only when the canonical
history proves the relationship (`basis: outcome_transition`):

1. the invocation's own `transition_selected` (carrying its `invocation_id`);
2. the `state_entered` of that transition's target;
3. the first `invocation_started` in that visit, which is a command verifier.

Being merely the next verifier in the run is not enough. If a human continuation, a budget route, a resume or a reopen
lies in between, nothing is linked. Without a proven link the value is `null`, shown as "Not executed".

## Read API and CLI

- **Read-only.** No writes, backfill, repair, Git, network or provider calls.
- **Order.** Invocations are listed in canonical order (`events.jsonl`). The default text output lists failed
  invocations first.
- **Detail.** `--invocation` takes a full id or `I<n>` and prints the bounded record. `--json` prints the records.
- **Legacy works.** Every canonical invocation is listed as `unavailable`. Without events the output is
  `no diagnostics available`.

## Out of scope

Live logs or watch, streaming, full stdout/stderr archives, prompt capture, token or cost accounting, OpenTelemetry,
remote telemetry, debug bundles, and retry, lifecycle or recovery policy changes.
