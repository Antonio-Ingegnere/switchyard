# Work logs and watch (OBSERVABILITY #1, slice 2)

```
switchyard work logs  WORK [--run R] [--state S] [--invocation ID|I<n>] [--last N] [--json]
switchyard work watch WORK [--interval SECONDS] [--last N] [--json]
```

These are a presentation layer over the durable trace (`runtime-trace.md`), joined with the diagnostics
(`provider-diagnostics.md`) by `invocation_id`:

```
events.jsonl -> trace (observability/events) -> switchyard.logs -> logs / watch
```

There is no new store, daemon, service, provider streaming or execution event. Observation never writes, backfills,
recovers or reconciles. It makes no Git, network or provider calls.

## Timeline

There is one compact line per trace event, ordered by trace `sequence` (never by timestamp). The columns are time
(local `HH:MM:SS`), state (or `WORK` for run-level events), label and detail.

| Trace event | Label | Detail |
|---|---|---|
| `run_started` | Started | flow, Switchyard version, base commit |
| `state_entered` / `state_exited` | Entered / Exited | visit number, human-wait marker |
| `routing_decided` | not shown as a line | Its provider/model appears on the next `Started` line, and it keeps its own NDJSON record. |
| `invocation_started` | Started | provider/model, agent, `I<n>`, attempt (or uncharged launch) |
| `invocation_finished` | Completed | outcome, duration |
| `transition_decided` | Transition | `-> TARGET (outcome or trigger)` |
| `budget_exceeded` | Budget | metric, observed/limit, route |
| `human_input_requested` / `_received` | Waiting / Answered | request id, target |
| `run_resumed` | Resumed | resume kind, target |
| `run_interrupted` | Failed + `Invocation I<n> \| Diagnostic available/unavailable` + `WORK Interrupted` | classification, exit code or timeout; reason; resume available or not |
| `run_finished` | Finished | terminal state, run duration |

`logs --invocation I<n>` adds the existing diagnostics detail renderer for that invocation, or
`Diagnostic unavailable`. Only fields inside the diagnostics safety boundary are shown: no prompts, source, raw
streams, structured payloads or credentials. Text is stripped of control characters, so no ANSI sequences are ever
emitted, whether or not stdout is a TTY.

A work without a trace prints `No trace events available.`

## Cursor

`TraceCursor` holds the next sequence to deliver.

- **Cheap polls.** Each poll first checks the event directory's mtime. An unchanged directory means nothing is listed
  or parsed. Otherwise only file names past the cursor are collected and only those files are parsed.
- **Contiguous prefix.** The cursor advances only over a contiguous prefix. A missing sequence, or a file that is not
  (yet) readable or valid, holds the cursor back; later events are never skipped. Publication temporaries (`.tmp-…`)
  never match.
- **Duplicates.** Each event is printed once. A controller restart writes new sequences after the old ones, and the
  existing event ids and sequences make duplicates impossible.
- **Logs.** `logs` reads the consistent prefix and warns if later events exist past a missing sequence.

## Watch lifecycle

On start, `watch` prints the header, the current status, and the last N events (default 10). It then polls at the
interval (default 1 s, clamped to 0.2–60 s) and prints only new events.

Status comes from a pure reader, `observed_status`. It reads the latest recovery `operation` record (and a recorded
QA decision) from the store manifests. It does not use `recovery_status`, which takes the controller lock and
reconciles a stale RUNNING record into an interruption. Both of those would write, or could make a starting controller
fail.

Status names are the existing `recovery_status` names:

| Phase | Status | Watch |
|---|---|---|
| `provisioning`, `running`, `restoring`, `suspending` | as named | Keeps watching. A RUNNING record whose provider process is gone gets a single note: "`work status` reconciles". It is not guessed or reconciled. |
| `waiting_for_human` | WAITING_FOR_HUMAN | Prints `next: switchyard work answer WORK <request_id> "<answer>"` and exits. |
| `interrupted` | INTERRUPTED | Prints `resume available: switchyard work resume WORK`, or `resume not available; inspect: switchyard work status WORK`, and exits. |
| `delivery_done` | the recorded terminal (`READY_FOR_MANUAL_QA`, `NEEDS_HUMAN`, …) or the recorded QA decision | Prints the next action and exits. |
| `reopened`, `suspended`, `accepted`, `rolled_back`, `rollback_running` | REOPENED, SUSPENDED, ACCEPTED, ROLLED_BACK, ROLLBACK_BLOCKED | Prints the next action and exits. |
| anything else | the phase, upper-cased | Reported as recorded, with a note, never guessed. |

Two rules govern exit:

- **Stable status.** `watch` exits only after a non-active status is seen on two consecutive polls with no new events
  in between. A controller writes its next record moments after a transition, so transient phases (for example
  `reopened` → `running`) do not end the watch.
- **Missing sequence.** While a sequence is missing, `watch` waits up to 5 further polls for the consistent prefix,
  then prints one warning and the final status.

Ctrl+C prints `switchyard: watch stopped; the work was not affected` to stderr and exits 130. Nothing is cancelled,
signalled or written.

## JSON

- **`logs --json`:** a JSON array of event entries.
- **`watch --json`:** newline-delimited JSON, one self-contained object per line, never a growing array. Every line
  has a `record` field:

| `record` | Fields |
|---|---|
| `event` | `sequence`, `event_id`, `event_type`, `work_id`, `run_id`, `state_visit_id`, `state`, `invocation_id`, `agent`, `provider`, `model`, `timestamp`, `observed_by`, `summary`. For `invocation_finished` and `run_interrupted` events with an invocation, it adds `diagnostic: {available, diagnostic_id}`. |
| `status` | `status`, `phase`, `active`, `state`, `next_action`, `note`. The first line also carries `work_id` and `trace_events`. The last line carries `final: true`. |
| `warning` | `warning` text, plus the held `sequence` for trace gaps. |

## Out of scope

TUI/curses, provider token streaming, raw stream retention, HTTP/WebSocket transport, database-backed logs, recovery
changes, new execution events, cost accounting, debug bundles and agent cancellation.
