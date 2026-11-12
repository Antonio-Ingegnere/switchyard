# Runtime trace (OBSERVABILITY #1, slice 1)

`switchyard work trace <work> [--json] [--run R] [--state S] [--invocation I] [--type T]` and
`switchyard.trace.trace_events(work_root, run_id=None, state=None, invocation_id=None, event_type=None)` show one
work's execution as a single ordered trace. That covers:

- what ran, and why;
- in which state, visit and rung;
- the agent, provider and model, and the budgets applied;
- the outcome and the transition that followed;
- the runtime, config and Git provenance.

The trace is **observability evidence, not lifecycle state**. The following stay authoritative and unchanged:

- `events.jsonl` (canonical runtime events);
- the recovery store (baseline, operation records);
- the artifact manifest;
- the QA, acceptance, integration, publication and PR stores.

The trace references and explains these records. It never replaces them.

## Correlation model

The trace reuses the runtime's own identities. It never mints parallel ones.

| Field | Meaning |
|---|---|
| `work_id` | The durable work identity: the delivery branch suffix. Non-isolated works use `baseline-<digest>`. |
| `run_id` | `run:<uuid>`. One run spans its interruptions, resumes, human waits and QA reopens. |
| `state_visit_id` | `<run>:visit:<n>`: one admitted entry into a state (vocabulary: *state visit*). Re-entering an interrupted visit keeps the id. A revisit, a reopen or a human continuation gets the next `n`. |
| `invocation_id` | `<run>:invocation:<n>`: one executor launch (vocabulary: *invocation*). A retry after a provider failure is a new invocation in the same visit. Ids are never reused. |
| `invocation_in_visit` | The launch ordinal within the visit. |
| `semantic_attempt` | The ordinal of the *charged* invocations within the visit (vocabulary: *attempt* = semantic retry). A proven infrastructure failure is a launch, not an attempt, so its value is `null`. |
| `turn` | Structured-output repair happens inside one provider process (one invocation). The runtime does not durably count those turns, so `turn` is `null` (unknown, never invented). |
| `state` / `runtime_state` / `rung` / `agent` | The logical state, the compiled runtime state, the rung index and the execution profile. |

## Events

Common envelope (schema 1):

- `schema_version`, `event_id`, `event_type`, `sequence`;
- `work_id`, `run_id`, `state_visit_id`, `invocation_id`;
- `state`, `runtime_state`, `rung`, `agent`, `provider`, `model`;
- `timestamp` (metadata only);
- `observed_by`: `runtime` or `recovery_backfill`;
- `source`: `{log: events.jsonl, index, kind}`, the canonical event this one projects;
- `runtime`: `{switchyard_version, config_sha256, flow}`;
- `payload`.

Each trace event projects exactly one canonical event:

| Canonical event | Trace event |
|---|---|
| `run_started` | `run_started`; payload carries `provenance.runtime` and `provenance.git` |
| `state_entered` | `state_entered` (visit number, visit limit) |
| `invocation_started` | `routing_decided` + `invocation_started` (ordinals, budget view, worktree/head) |
| `invocation_finished` | `invocation_finished` (outcome, `outcome_category: semantic_outcome`, times, `duration_ms`, configured timeout, `artifact_refs`, `diagnostic_id`; see `provider-diagnostics.md`) |
| `budget_exceeded` | `budget_exceeded` (metric, limit, observed, route) |
| `transition_selected` | `transition_decided` (source state and visit, target state and rung, trigger, outcome) |
| `state_exited` | `state_exited` |
| `human_input_requested` / `_received` | same name (request id = visit id) |
| `run_resumed` / `human_input_resumed` | `run_resumed` (`resume_kind: interrupted_visit / recover_to / human_input`; live context: from_terminal, resume_to, qa_fix_round, candidate identity) |
| `run_error` | `run_interrupted`: the durable `interrupted` operation record's facts, plus `diagnostic_id` |
| `run_finished` | `run_finished` (terminal) |

`run_interrupted` carries the interruption facts using the existing taxonomy:

- `reason`: `provider_failure`, `provider_timeout`, `execution_interrupted` or `qa_evidence_undeliverable`;
- `outcome_category`: the provider-failure classification (`timeout`, `rate_limit`, `authentication`,
  `provider_unavailable`, `transport_failure`, `invalid_stage_result`, `provider_exit`) or the reason;
- `semantic_invocation`, `exit_code`, `duration_ms`, `resume_safe`;
- a 256-character redacted `diagnostic`.

## Routing evidence

`routing_decided` records:

- the requested state and rung, role and agent;
- provider, model and effort;
- the workspace mode, the shell setting and the capabilities;
- the configured timeout, context mode and escalation kind;
- `policy: compiled_workflow_rung` and `policy_identity` (`@v2:<flow>:<state>:<rung>`);
- `fallback_used: false`. There is no fallback routing to report.

## Budget evidence

`invocation_started.payload.budget` holds:

- `semantic_charged`;
- `run_invocations` (`used_before`, `used_after`, `limit`);
- each `invocation_scopes` entry (scope, `per_visit_of`, `used_before`, `used_after`, `limit`);
- `state_visits` (`used`, `limit`);
- `infrastructure_failures` (agent, `used`, `limit`).

The counters are the same reconstruction the runtime performs on resume. Invocations voided as proven
infrastructure failures are not charged. Live, the runtime commits capacity at launch. A launch that later
proves to be an infrastructure failure (`run_interrupted.semantic_invocation: false`) is excluded from the next
launch's `used_before`, and its agent's `infrastructure_failures.used` goes up.

## Storage, ordering, identity

- **Files:** `<work>/observability/schema.json` plus one immutable file per event,
  `<work>/observability/events/<sequence:06d>-<event_id>.json`. Each file is written to a temporary file,
  fsynced, then exclusively hard-linked into place. Nothing is ever overwritten or rewritten.
- **Ordering:** `sequence` is work-local, dense and deterministic. The projection of canonical event k
  depends only on the canonical prefix, so a live write and a later backfill give the same sequence.
- **Identity:** `event_id = sha256(work, run, canonical index, canonical kind, trace type)[:32]`, with no
  timestamps. Re-emitting an already-durable event is recognized and skipped. Restarts, retried finalization
  and repeated backfills produce no duplicates.

## Failure isolation and backfill

- **Isolation:** trace writes happen after the authoritative write (`events.jsonl` fsync, operation record).
  They are failure-isolated: an error stops tracing for that drive and nothing else. No reroute, no retry, no
  budget charge, no lifecycle or result change. The test suite injects trace failures to show this.
- **Deferred interruption:** `run_interrupted` is deferred until the `interrupted` operation record exists.
- **Backfill:** when a work is next driven (resume, answer, qa-fix, reopen), canonical events without trace
  events are projected and written with `observed_by: recovery_backfill`. Only what the durable records prove
  is written. Live-only facts that were never captured, such as `artifact_refs` and resume context, stay
  `null`. Durations come only from durations persisted in `events.jsonl`.
- **Read paths never backfill:** `work trace` and `recovery_status` never backfill.

## Sensitive data and bounds

- **Never stored:** prompts, provider stdout/stderr, artifact contents, source files, request text,
  environment values, tokens, or remote URLs.
- **Artifact references:** kind, name, version, path, SHA-256 and size. At most 20 per invocation, with a
  `truncated` marker beyond that.
- **Diagnostics:** URL userinfo and token-like strings are redacted, and the text is capped at 256 characters.

## Status, legacy, scope

- **Status:** `recovery_status()['observability']` = `{trace_events, last_sequence, last_event}`, computed
  locally from file names and the last file.
- **Legacy works:** a work without `observability/` reports no trace events (`no trace events available`).
  Nothing is fabricated.
- **Other operations:** suspend, accept, integration, push and PR do not write trace events in this slice.
  Adopt and transplant create the new work's own trace; the source work's trace is never touched.
- **Out of scope:** live watch, provider log retention, debug bundles, cost accounting, export.
