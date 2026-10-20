# Explicit delivery recovery (RECOVERY-001)

Recovery wraps the existing V2 compiler, context transport and runtime. It does
not retry automatically, fabricate a StageResult or restore a provider session.
New operations use the installed `switchyard-runtime` package, version 2.2.0 (declared
terminal recovery since VERIFICATION-RECOVERY-001).
The advanced `execute_v2_workflow` API remains available without recovery.

## Public API

All recovery entry points live in `switchyard.recovery`:

```python
execution = execute_recoverable_v2_work(
    config_text, "feature", human_request, fresh_work_root, provider_runtime_config)
# A known provider failure raises after recording an interruption.
status = recovery_status(work_root)
# Only on explicit human request:
execution = resume_v2_work(work_root, current_config_text, provider_runtime_config,
                           reason="optional bounded human annotation")
status = rollback_v2_work(work_root, reason="Wrong direction")
```

After a successful delivery, the consumer creates the usual external QA candidate
and calls `attach_qa_candidate(work_root, qa_root)`. Accept/reopen remain separate
human decisions through the existing lifecycle APIs. Recovery owns no commit.

## Timeouts and budgets

`agents.<name>.timeout` is seconds: a finite positive number, never a boolean or
string. Its default is 600 for compatibility; projects should set it explicitly.
SimpleConfig lowers it to the selected execution profile's `timeout_seconds`.
The resolver applies that deadline to the actual model or command process. Advanced
profiles without a deadline retain `ProviderRuntimeConfig.timeout_seconds` fallback.
`config show` displays the effective timeout without launching providers.

A semantic invocation is one whose stage adapter returned a parsed StageResult. It
consumes its ordinary run/state/rung budgets, including when Switchyard later rejects
the result or fails to persist it. An infrastructure failure is a provider process
that ended before any StageResult: nonzero exit, signal, timeout, transport/start
failure, or output that is not a valid StageResult. It changes no artifact, is
recorded on the interruption (`semantic_invocation: false`, `invocation_id`, bounded
`provider_failure` facts: classification, provider, model, agent, exit code, timeout
flag, duration, redacted output tail) and is never charged to semantic budgets.
Classification is `timeout`, `provider_exit`, `rate_limit`, `authentication`,
`provider_unavailable`, `transport_failure` or `invalid_stage_result`.
`invalid_stage_result` also covers a provider that answered but exhausted StageOutputContract
repair (Claude `error_max_structured_output_retries`); its output tail ends with the
machine-readable `stage_result_errors` records.

Infrastructure failures have their own bound: `agents.<name>.infrastructure_failures`
(positive integer, default 2) per agent per work. Resume refuses with
`RESUME_BLOCKED: infrastructure_budget_exhausted` once that agent has reached it, so
the default tolerates one failed provider run and refuses after the second. There is
no automatic retry; every further provider process still needs a human resume.

Resume re-enters the interrupted invocation's already-admitted visit, with a new
invocation identity and cumulative counters reconstructed from the same event history,
excluding proven infrastructure failures (`run(..., void_invocations=...)`; the runtime
accepts only unfinished invocations that ended in `run_error`). It never repeats
successful states. `run_resumed` follows the retained `run_error`; Event fields,
routing and budget admission rules are unchanged. Cross-process elapsed durations
are not invented. The recovery controller rejects exhausted budgets before launch.

Histories written by Switchyard 2.2 have no `semantic_invocation` field. Their
`provider_failure`/`provider_timeout` interruption with `resume_safe: true` is proof
of an infrastructure failure: 2.2 raised that class only inside stage adapters before
a StageResult, and `resume_safe` required an unchanged artifact manifest. Together with
the retained unfinished `run_error` of the same state/rung, the latest such record is
read as uncharged. Resume appends the inference as `inferred_interruption` on its new
operation record; history is never edited. Earlier 2.2 interruptions that were
already resumed stay charged.

Only agent timeouts may change between initial execution and resume. The baseline
holds the original semantic configuration; normalized YAML comparison removes only
`timeout`. Provider, model, effort, permissions, topology, artifacts, lifecycle and
budgets remain frozen. A semantic change requires a new delivery. Config is still
the one editable project file; baseline/config snapshots are immutable evidence.

## Human recovery from NEEDS_* terminals

`NEEDS_HUMAN` and `NEEDS_REDIAGNOSIS` stop automation. A run there stays stopped until
a human calls `resume_v2_work`. It continues only if the work's frozen configuration
declares the target on that terminal:

```yaml
flows:
  feature:
    states:
      VERIFY:
        run: [{agent: tests, budget: {invocations: 2}}]
        budget: {visits: 2}
        # ...
      NEEDS_REDIAGNOSIS: {terminal: true, resume_to: VERIFY}
```

No other terminal may declare `resume_to`; READY_FOR_MANUAL_QA belongs to QA. Callers
cannot name a state: there is no goto, rewind or `--state`. Without a declaration,
resume refuses with `RESUME_BLOCKED: no_declared_recovery_route` and changes nothing.

Recovery continues the same work and run. `run_resumed` records the terminal and the
runtime target; the closed terminal visit remains in history. The target is entered as
a new admitted visit, so visit, rung, state and run budgets apply from the reconstructed
counts and are never reset. Reserve capacity explicitly, as above. Completed states,
artifacts and handoffs remain; nothing before the target reruns. Resume refuses before
launch when capacity is exhausted, a consumed input of the target is missing, the
artifact manifest moved since the final checkpoint, the configuration changed beyond
timeouts or the worktree drifted from the checkpoint (`RESUME_BLOCKED`); human edits
are never overwritten. The optional `reason` (at most 4096 UTF-8 bytes) is stored on the
operation record with the terminal and target. It never affects routing. A run that
reaches READY_FOR_MANUAL_QA after recovery yields an ordinary QA candidate.
`recovery_status` reports `resume_to` and `resume_allowed`. Work whose baseline
configuration declared no route, including all earlier work, is not recoverable this way.

## Durable state and interruption

The delivery workspace retains its request, manifest, artifacts, handoffs and
events. A nested `recovery/` WorkWorkspace stores versioned JSON baseline,
checkpoint, operation, QA reference and rollback intent records. Content-addressed
SHA-256 objects store binary baseline bytes and the original Git index. They are
recovery data, not provider transcripts or event payloads. Access requires normal
worktree confidentiality: pre-existing Git-visible files may contain private data.

The baseline precedes every provider invocation and captures canonical target/Git
directory, branch, base HEAD, Git-visible paths, existence, SHA-256, Git mode, file
permissions and semantic index fingerprint. Clean tracked bytes refer to immutable
Git blobs; dirty and untracked bytes are stored verbatim. Deletions are represented
by absence. Symlink targets are stored without following them; symlink parents,
submodules, conflicts and unsupported index flags fail closed using existing Git
guards. Ignored untracked files are outside the recovery contract.

The controller holds a work lock during execution/recovery. Provider transport
starts a POSIX process group, kills that group on completion/failure and waits for
the direct child before publishing termination. This prevents ordinary tool children
from continuing to write after timeout. Detached/escaped processes are not a supported
provider contract; this is not general process supervision or a cancel operation.
Each completed invocation gets a worktree checkpoint, including abnormal termination.
Partial source edits remain; rejected or absent StageResult outputs are not committed.

Known failures record `interrupted`, logical state/rung and bounded reason:
`provider_timeout`, `provider_failure` or `execution_interrupted`. Status adds
`semantic_invocations_used`/`_limit` (tightest scope of the visit to run next),
`semantic_budgets`, `infrastructure_failures_used`/`_limit`, `resume_to` (the
interrupted logical state when resumable), `resume_blocked` and `provider_failure`. The consumer's
navigation pointer reflects this authoritative state. No raw stderr goes into that
pointer. Status reports resume/rollback eligibility; those flags are advisory and
each operation revalidates. Resume verifies repository/HEAD/index, checkpoint bytes
and unchanged artifact manifest before rebuilding normal durable context.
The tracked config file may differ only by validated timeout edits. Other worktree
drift blocks resume; conflicting paths are bounded, never merged or overwritten.

## Rollback and QA

Rollback is permitted for interrupted or completed pre-accept work, including
READY_FOR_MANUAL_QA, REOPENED and COMMIT_BLOCKED. Active controllers/providers refuse.
ACCEPTED refuses with guidance to use ordinary Git revert or corrective delivery.
Base HEAD movement also refuses; no history is reset.

Owned paths are the difference between baseline and last checkpoint. Each must
still match that checkpoint before restoration. Human edits to those paths produce
ROLLBACK_BLOCKED. Unrelated later files are preserved. Restoration uses verified
baseline bytes/modes: clean tracked Git blobs, exact pre-existing dirty/untracked
bytes, restored deletions and removal of delivery-created paths. Binary files,
symlinks and permissions are preserved. Original staged content is restored through
an exclusively owned index lock only when delivery changed the index; unexpected
later staging blocks. There is no reset-hard, clean, checkout-all or restore-all.

Pending QA is durably finalized ROLLED_BACK before source restoration via
`withdraw_qa_candidate`. Existing finalized reopen/blocked decisions remain immutable.
Candidate history survives and cannot later be accepted. The operation appends
ROLLED_BACK with timestamp, work identity through workspace, prior state, optional
bounded reason and restored-path count. No history or baseline is deleted. Adeo
retires the current work logically; its pointer can display the last result while
allowing a new work ID immediately.

Timeout retuning can change tracked `.switchyard/config.yaml` during the same run.
QA permits precisely this configuration file as ordinary reviewed content; all
other protected state paths remain rejected. Candidate paths/fingerprints, drift
checks, private-index commit and human acceptance authority are unchanged. Review
the config change too. Pre-existing staging still blocks candidate creation; rollback
remains available if QA capture cannot complete.

## Failure boundaries and limitations

Recovery requires a POSIX host (flock) and exclusive cooperative worktree ownership
while providers run. It cannot distinguish an unrelated concurrent human write from
a provider write at checkpoint capture. The index lock excludes cooperating Git
writers only during restoration; it is not a filesystem transaction.

Manifest publication retains WorkWorkspace's fail-closed partial-write behavior.
Baseline objects can precede a manifest entry; orphaned or incomplete records require
inspection. A failed provider with durable `run_error`, unchanged output manifest
and confirmed termination is resumable. An abrupt controller crash with uncertain
output/checkpoint state is marked INTERRUPTED once its lock and recorded process
are dead, but refuses automatic reconstruction or destructive rollback. Torn event
logs or output publication ambiguity likewise require manual recovery. There is no
claim of arbitrary crash-resume or automatic repair of historical work without a
baseline. PID reuse conservatively keeps work active rather than racing a process.

Rollback writes an intent before mutation. After interruption, it may continue only
if each owned path still equals its checkpoint or exact baseline state. A partially
written restoration, missing blobs or unresolved QA intent blocks for inspection.
QA withdrawal can remain durable even if later restoration blocks; it must not be
reversed into acceptability. Nothing automatically deletes recovery or QA history.
No provider retries, push, scheduling or project/epic orchestration are introduced.

## Answering an explicit human wait

Human wait is not provider interruption. A successful clarification result remains
charged, and the run pauses through its declared wait state. Legacy NEEDS_HUMAN
terminals retain their old behavior unless the flow explicitly declares a wait.

```python
from switchyard.recovery import answer_v2_work, recovery_status
status = recovery_status(work_root)
execution = answer_v2_work(work_root, status['human_input_request_id'], answer_text,
                           current_config_text, provider_runtime_config)
```

The operation locks the existing work and validates its pending ID, frozen semantic
configuration, declared target, worktree/checkpoint integrity, required inputs and
remaining budgets. It publishes the answer before executing. It never accepts a
state/goto argument. Source snapshots, artifacts, run ID and history remain in place.

CLI equivalents (target repository is read from the existing recovery baseline):

```sh
switchyard work status <work-root>
switchyard work answer <work-root> <request-id> "Yes, preserve existing saved queries."
switchyard work resume <work-root>
switchyard work rollback <work-root>
```

Answer/resume read `<target>/.switchyard/config.yaml` by default; `--config PATH`
selects the current file. `--claude-executable` / `--codex-executable` optionally
select local binaries, just as ProviderRuntimeConfig does. No provider is launched
by status. The API retains support for caller-supplied aliases/environment.

Status exposes `phase: waiting_for_human`, the actual wait `state`, no terminal,
`human_input_pending`, request ID, round, exact question/blockers, `resume_to`,
`answer_allowed`, and `next_action: answer required`. Generic resume refuses with
`human_input_required` and exposes the request on the exception. After answer
publication, status can report `human_answer_ready` and permit resume. An unrelated
subsequent provider failure follows ordinary recovery with the answer still in
context. Infrastructure counters and all cumulative semantic budgets are preserved.
Rollback remains available while waiting and retains question/answer/event history.

A fully published wait checkpoint, question and event reconstruct the wait after a
controller restart, including a crash before the final operation marker. An answer
published before execution likewise survives restart. Existing fail-closed limits
still apply to torn manifests/events or a controller dying midway through runtime
resume events without a reconstructible provider checkpoint; this is not arbitrary
crash repair. Waiting status and answering require no provider session memory.
