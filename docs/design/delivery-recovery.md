# Explicit delivery recovery (RECOVERY-001)

Recovery wraps the existing V2 compiler, context transport and runtime. It does
not retry automatically, fabricate a StageResult or restore a provider session.
New operations use the installed `switchyard-runtime` package, version 2.3.0 (declared
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

## Delivery isolation (#3)

A new delivery runs in its own Git branch and worktree; `provider_runtime_config.working_directory`
names the user's **source checkout**, which delivery execution never writes. Switchyard requires
Git (there is no filesystem-copy isolation).

- **Clean source.** The source checkout must be a branch checkout with no staged, modified or
  untracked (not `.gitignore`d) paths: `DELIVERY_BLOCKED: dirty_source_worktree …` lists them. Only
  `.switchyard/config.yaml` is exempt (as in drift checks: the work runs on the config text frozen in its
  baseline). Nothing is stashed, committed, copied or reset.
- **Provenance** (`isolation` in the baseline, shown by `recovery_status`): `repository` (common Git dir),
  `source_worktree`, `source_branch`, `base_sha` (full commit SHA of the source `HEAD`),
  `delivery_branch` = `refs/heads/switchyard/<work-id>` (work id = work-root name, which must be a safe
  ref component), `delivery_worktree` = `<work_root>/workspace`. The work root may not lie inside the
  source checkout. Status also reports `isolated` and, when the recorded worktree no longer matches,
  `isolation_blocked`.
- **Provisioning order.** Preflight (clean source, branch and path absent; no writes) -> `provisioning`
  operation holding the provenance -> branch created at `base_sha` (create-only ref update) ->
  `git worktree add` (repository hooks disabled) -> baseline of the worktree -> `running`. Retrying the
  same start after a crash anywhere before `running` adopts the branch/worktree only if they still match
  exactly (branch at `base_sha`, clean worktree of the same repository on that branch); otherwise it
  refuses (`delivery_branch_conflict`, `delivery_worktree_not_owned`, …). A fresh start refuses an
  existing branch (`delivery_branch_exists`) or path (`delivery_worktree_path_exists`). Nothing is ever
  force-reset or deleted.
- **Routing.** Every model stage, the verifier command, checkpoints, rollback and QA candidates use the
  recorded delivery worktree: the runtime replaces the provider config's working directory, which must be
  either the source checkout or the delivery worktree (`provider_workspace_mismatch`). Candidates are
  relative to `base_sha`; a candidate captured elsewhere is refused at attach
  (`qa_candidate_not_from_delivery_worktree`).
- **Base drift.** Advancing or editing the source checkout after the start has no effect: the work stays
  based on `base_sha`; it is never rebased.
- **Recovery** validates before acting: the worktree exists, belongs to the recorded repository, has the
  delivery branch checked out, and descends from `base_sha` (`delivery_worktree_missing`,
  `delivery_worktree_not_owned`, `delivery_branch_mismatch`, `delivery_base_mismatch`). A deleted or
  repointed worktree is never recreated.
- **Accept** (`accept_v2_candidate`) produces exactly one commit C on the delivery branch: parent = the
  candidate's `base_head` (the recorded base, never a newer source HEAD), tree = exactly the candidate's
  paths/bytes/modes, message = the candidate subject plus `Switchyard-Work`, `Switchyard-Candidate` and
  `Switchyard-Candidate-Sequence` trailers (no request or QA text). The source checkout and its branch are
  untouched; integrating the delivery branch is a later slice.
  - Before any durable step it validates the isolation provenance, that the candidate was captured in the
    delivery worktree on the delivery branch from a descendant of `base_sha`, that it is the current
    candidate with no open QA round, and that the worktree still exactly matches it. Drift is refused with
    `ACCEPT_BLOCKED: candidate_worktree_drift: <reason>` and records nothing; files are never restored or
    cleaned.
  - Durable order: `qa_intent` started -> commit object -> `qa_intent` prepared(C) -> branch CAS
    `base_head` -> C -> index -> `qa_decision` ACCEPTED(C) -> operation `accepted` with
    `accepted_candidate_identity`, `accepted_candidate_sequence`, `accepted_commit_sha`, `delivery_branch`,
    `base_sha` (shown by `recovery_status`). The delivery worktree is clean afterwards.
  - A retry after a crash converges on one commit: nothing published -> accept now (an unpublished commit
    object stays unreferenced); branch at the prepared C and C provably is this acceptance (parent,
    paths, content, modes, message) -> reuse C, install its index, record it; anything else on the branch
    -> `accept_reconciliation_ambiguous`, never reset. A decision recorded without the work record is
    reported with `acceptance_finalization_pending` and finished by the retry without Git. Under the work
    lock, lock files left by a dead controller in the candidate and delivery worktree are removed.
  - Accepting again is refused with `already_accepted: <sha>`; a Git failure yields `COMMIT_BLOCKED` with no
    accepted commit, as before.
- **Legacy.** Work whose baseline has no `isolation` (and `isolation=False` starts) keeps the in-place
  semantics: the caller's checkout, dirty baselines and in-place rollback.

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

## Workflow terminal recovery

A terminal stops automation regardless of its name. A run there stays stopped until
a human calls `resume_v2_work`. It continues only if the work's frozen configuration
explicitly makes it recoverable by declaring a target:

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

Any terminal name may declare `resume_to`; once a QA candidate is attached, QA still
owns its decision and ordinary workflow recovery is refused. Callers
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
remaining budgets. It publishes the answer before executing. An optional `resume_to` argument selects only among the frozen wait
contract's declared targets; it cannot jump to an arbitrary state. Source snapshots, artifacts, run ID and history remain in place.

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

## Frozen lifecycle contract

New recovery baselines store `lifecycle_contract_version: 2` alongside the exact
original project config. That immutable pair defines lifecycle semantics for status,
answering, compilation and recovery. Optional project `version: 2` documents the
current syntax; omission also means explicit semantics for new deliveries. Only
validated timeout changes are permitted during continuation. Editing a terminal or
wait declaration in the target project never changes status for existing work.

Baselines without the marker belong to the previous contract. Only this frozen-work
path invokes `legacy_config.read_legacy_project`, which reproduces the former
implicit known terminals and restricted recovery declarations in memory. Existing
human waits stay explicit. It never rewrites old config, artifacts, manifests or
events. The same ambiguous config passed to new delivery or `config validate` fails;
there is no public legacy switch for starting new work. Unknown frozen versions fail
closed. The advanced bundle stays at version 2 because its existing schema already
requires explicit state declarations; the change needing version dispatch is the
project-config interpretation stored in recovery baselines.

Status adds `workflow_state`, `state_kind`, `workflow_terminal`, `waiting`, `wait_for`,
and `declared_resume_to`, all derived from the frozen state. Existing `state` and
`terminal` fields retain their previous meanings; `terminal` is the operation result
(e.g. `INTERRUPTED`, a workflow terminal, or an external QA decision).
`recovery_source` distinguishes `workflow`, `human_input`, and
`runtime_interruption`; ordinary active/rollback operations have no recovery source.
Runtime interruptions do not create YAML states. `resume_allowed` still combines
the declared route or proven interruption with existing budget/recovery guards.

Wait != terminal. Recoverable terminal != provider interruption. Human answer !=
generic resume: only a durable answer satisfies an unanswered human wait.
Semantic clarification charges, per-visit epochs, cumulative budgets, source snapshots,
and provider infrastructure accounting are unchanged. Existing events continue to
express pauses (`human_input_requested`), terminal completion (`run_finished`), and
runtime failure (`run_error`); no historical event is reclassified or rewritten.


## Selectable human continuation (HUMAN-TRANSITION / issue #18)

`answer_v2_work(work_root, request_id, answer, config_text, provider_runtime_config,
resume_to=None)` extends the existing API. With one allowed target, omission
selects it. With multiple targets, omission or a nonmember fails with an error
listing the allowed targets. CLI uses `work answer ... --resume-to DIAGNOSE`.
Validation against the frozen contract, destination inputs and cumulative budgets
precedes answer publication. The runtime independently checks the selected compiled
entry against its State declaration. Providers only return semantic outcomes.

Pending status includes `human_input_request_id`, `question`, `blockers`,
`origin_state`, `origin_outcome`, `resume_targets`, and `selected_resume_to` (null
until answered). `resume_to` remains a scalar for a single-target wait and is null
for multi-target waits, including after selection. The decision is reported through
`selected_resume_to`, `human_answer`, durable answer records, and received/resumed
events. Ordinary post-wait status retains the latest decision and its origin.

`resume_allowed` means the work has a valid supported continuation path with
remaining budget, for both single- and multi-target waits. It does not necessarily
mean `switchyard work resume` is immediately executable. When
`human_input_pending: true`, `next_action` is `answer required`; generic resume
remains rejected with `human_input_required` until a valid answer and any required
target decision are persisted. A recorded answer changes `next_action` to `resume`.
Pending input is a prerequisite, not a `resume_blocked` reason. Budget refusals set
`resume_allowed: false` and `resume_blocked` explicitly.

`target_blockers` reports budget refusal per considered target; `accounting_target`
identifies the target used for the legacy accounting projection. Once answered, only
the selected target is considered. There is no separate `continuation_allowed` flag.
This intentionally corrects the old single-target pending `resume_allowed: false`
projection. Clients that used that flag as permission to call generic resume must
also inspect `human_input_pending` or `next_action`. Answer API compatibility and
server-side answer requirements are unchanged.

Status and selected-target validation read the frozen baseline workflow. A newer
project configuration cannot add, remove or replace allowed routes: semantic config
changes are rejected before publication. Permitted timeout retuning affects execution
settings only; it does not supply the target-validation contract.

New human records use `schema_version: 2`:

- `human_request` adds ordered `resume_targets` and retains `resume_to` as a scalar
  for one target or null for multiple targets. It preserves question, blockers,
  summary, origin state/rung/invocation/visit/outcome, handoff sequence, work/run/flow,
  round, stable request ID and timestamp.
- `human_answer` adds `selected_resume_to` to answer text, request ID, sequence,
  actor, work/run provenance and timestamp. The request stays immutable.

Readers dispatch explicitly on version. Schema-v1 requests and answers keep their
original single-target semantics; new answers to old requests use v2 and preserve
all prior records byte-for-byte. Unsupported versions and inconsistent route or
answer provenance fail closed. No historical artifact is rewritten.

Only the latest answered pair relevant to the selected flow/state enters context.
An allowed but unselected destination receives no new pair. Waiting and answering
keep run identity, artifacts, history, visit counts, semantic charges and provider
failure accounting. No additional allowance is granted by selection.

Workflow bundle version remains 2 and project configuration syntax remains additive.
No new dependencies or Python requirement changes are needed. Deploy the updated
runtime/CLI together before using lists or schema-v2 human records: older releases
do not support multi-target contracts or their null-target pending events. Downgrading
a work that has new human records is unsupported; old records remain readable by
the updated release. Package version is unchanged by this implementation.

## Corrective manual QA (`qa-fix`)

A work in `READY_FOR_MANUAL_QA` with an attached candidate can be rejected with free-text
feedback and continue **as the same work** (same run, events, artifacts and budgets):

```text
READY_FOR_MANUAL_QA -- qa-fix <feedback> --> REOPENED -> <resume_to of the QA terminal> -> ... -> READY_FOR_MANUAL_QA
```

- The route is declared in the frozen workflow: `READY_FOR_MANUAL_QA: {terminal: true, resume_to: DIAGNOSE}`.
  Without it (or with `lifecycle.reopen: false`) `qa-fix` is refused before anything is written.
  As before, plain `resume` follows that route only while no candidate is attached.
- Budgets are not reset. The target state needs remaining visits and invocations
  (e.g. `rung_budget_scope: visit`, `budget: {visits: 2}`); otherwise `qa-fix` is refused and the candidate stays QA-ready.
- Feedback (≤ 8192 bytes) and a bounded summary of the rejected candidate are stored in `<work>/qa_fix`
  (`qa_fix_request`). While that round is open, every non-command stage receives only this record
  (`MANUAL QA REJECTION` section) plus the request; no transcript is replayed.

Durable order of `qa-fix` (each step is finished, never repeated, by retrying the same command or `resume`):

1. preflight: route, configuration, checkpoint drift, history, budgets (no writes);
2. `qa_fix_request` — the durable intent; status now reports `REOPENED`, accept is refused;
3. candidate #N finalized `REOPENED` (`qa_intent`, `qa_feedback`, `qa_decision`; an interrupted reopen is completed);
4. operation `reopened`;
5. corrective run from the declared entry state (an interruption is an ordinary `interrupted` resume).

When a corrective run reaches `READY_FOR_MANUAL_QA`:

1. operation `interrupted` / `candidate_publication_pending` (status `INTERRUPTED`, never QA-ready);
2. candidate #N+1 built in a temporary directory with `sequence` and `supersedes`, then atomically renamed
   to `<work>/qa/candidate-N+1` (identity advances even for identical bytes);
3. latest `qa_reference` → candidate #N+1;
4. `qa_fix_resolved` closes the round;
5. operation `delivery_done` / `READY_FOR_MANUAL_QA`.

A failure or crash in 2–4 leaves `candidate_publication_pending`; `resume` rechecks checkpoint drift and
history, reuses an already renamed candidate only if it still matches the worktree, and executes no stage.
`rollback` stays available. `accept` (`accept_v2_candidate` / `switchyard work accept`) accepts only the
current candidate; rejected candidates are finalized and cannot be accepted.

CLI: `switchyard work qa-fix <work_root> "<feedback>" [--config FILE]`, `switchyard work accept <work_root>`.

### Repeated QA rounds

Each rejection is a durable, numbered round in the same work; rounds never reset budgets.

- Round N is `qa_fix_request` version N (`round`, `feedback`, `rejected_candidate`) plus
  `qa_fix_resolved` version N (`round`, `rejected_identity`, `superseding_identity`, `sequence`,
  `superseding_workspace`; the first committed slice wrote these without `rejected_identity` and
  `superseding_workspace`, which readers derive from the request and the `qa_reference` history).
- Both stores are append-only; a resolved round is never rewritten. Round numbering and
  request/resolution pairing are validated on every read.
- Lineage is a chain: candidate N+1 has `sequence: N+1` and `supersedes` = the candidate rejected in
  round N. `qa-fix` refuses (`qa_lineage_mismatch`, before any write) to open round N+1 against any
  candidate other than the one round N produced.
- Corrective stages receive only the open round's record (its feedback and rejected-candidate summary);
  earlier rounds stay in the store and are not replayed.
- `qa_history(work_root)` returns every candidate (sequence, identity, supersedes, decision, workspace)
  and every round (rejected → produced, feedback, open). Status reports `current_candidate_sequence`,
  `current_qa_round` and `qa_round_open`.

### QA findings

A rejection carries one or more findings: `qa-fix <root> "text"` (one finding) or
`qa-fix <root> --finding "A" --finding "B"` (`qa_fix_v2_work(..., None, ..., findings=[...])`, at most 20).

- Findings are part of the round's single durable rejection intent (`qa_fix_request` schema 3:
  `findings: [{id, round, description}]`), so they are created atomically with the round.
  IDs are assigned once at that write: `qa-<round>-<n>`; they are never derived from position on read.
- `feedback` on the request (and the rejected candidate's `qa_feedback`) is the single finding verbatim,
  or `[qa-R-n] text` lines for several.
- Status is derived from append-only records, never stored or rewritten:
  - `open` — the round's request exists and its round is not resolved;
  - `fixed` — the round's `qa_fix_resolved` exists (written only after its produced candidate is current);
  - `verified` — the produced candidate is in the supersedes chain of a candidate whose `qa_decision` is
    `ACCEPTED`. Accepting candidate #N therefore verifies every fixed finding in its lineage.
- A later rejection always creates new findings in its own round; earlier findings are never reinterpreted.
- Pre-findings requests (schema 1) are read as one finding `qa-<round>-1` with the old feedback, without rewriting.
- Corrective model stages get a `MANUAL QA FINDINGS` section listing only the open round's `[id] description`.
- `qa_history()` exposes `findings` (with `status`) per round; status adds `qa_open_findings`,
  `qa_fixed_findings` and `qa_verified_findings` counters.

### QA evidence

Findings may carry durable evidence files (screenshots, logs, any type):
`qa-fix <root> "text" --attach shot.png [--attach console.txt]`, or with exactly one `--finding`.
Several `--finding` with `--attach` is refused as ambiguous; the API maps files per finding:
`findings=[{'description': ..., 'attachments': [path, ...]}, ...]`.

- Inputs are untrusted: each must be a regular readable file (≤ 10 MiB, ≤ 10 per finding,
  ≤ 20 and ≤ 32 MiB per round). All files are read and validated before anything is written;
  later changes to the source files have no effect.
- Storage is outside the worktree and never committed: `<work>/qa/evidence/<id>/{metadata.json,<filename>}`.
  The filename is sanitized (`[A-Za-z0-9._-]`, no leading dots, never `metadata.json`); the directory is
  chosen only from the validated ID. MIME comes from magic bytes or a fixed extension table, else
  `application/octet-stream`.
- `id = ev-` + SHA-256 (first 128 bits) of the metadata: filename, MIME, size, content SHA-256, rejected
  candidate identity, QA round, finding ID and position. Objects are built in a temporary directory and
  published by atomic rename; an existing ID is reused only if its metadata and bytes verify.
- Order: validate → publish evidence → `qa_fix_request` (schema 4, `findings[].evidence: [{id, sha256}]`)
  → reject → corrective run. A crash before the request leaves only unreferenced immutable objects, which
  the same retry reuses; the request never references unpublished evidence.
- `qa_history()` lists each finding's verified evidence metadata (never bytes); `evidence_bytes(work, id)`
  returns verified bytes. Evidence changes no candidate, finding status, budget or transition.

### QA evidence delivery to corrective stages

While a QA round is open, every model stage receives that round's evidence, read-only, as
provider-neutral `StageInvocation.qa_evidence` parts (`EvidencePart`). They are rebuilt each invocation from
the open request's evidence references through the verified read path; nothing is copied into the store and no
identities are created. Earlier rounds' evidence is not delivered. Command stages (verifiers) receive none.

- Classes by durable MIME metadata: text (`text/*`, JSON, XML, JavaScript, YAML; strict UTF-8) and images
  (`image/png`, `image/jpeg`, `image/webp`). Anything else cannot be consumed.
- Text is shown inline under a `MANUAL QA EVIDENCE` section with finding ID, filename, MIME and SHA-256, at most
  8 KiB per file and 24 KiB per invocation, cut on a character boundary and marked
  `[TRUNCATED: showing first N bytes of M]`. It counts toward the stage context budget.
- Images are never prompt text: the section only labels them (`#k finding / filename (MIME, evidence ID, sha256)`).
  Each adapter translates image parts into its own transport.
- Capability: `switchyard.model_capabilities.IMAGE_INPUT_MODELS`, an audited table of exact resolved model IDs per
  provider adapter (Codex CLI 0.163.0 `exec --image`; the Claude adapter has no audited image transport).
  Codex receives read-only temporary copies of the verified bytes via `--image=<files>`; Claude and unaudited
  models refuse.
- Never silently dropped, and never at the cost of the human decision: `qa-fix` validates attachments only
  structurally (exists, regular, readable, limits, mapping) before writing. The rejection, finding, evidence and
  REOPENED candidate are then recorded normally. Before the first model stage whose provider/model cannot consume the
  evidence (unsupported type, invalid UTF-8 text, image without `image input`, or missing/tampered bytes) is
  launched, `EvidenceDeliveryError` stops it: zero provider processes, work `INTERRUPTED` with reason
  `qa_evidence_undeliverable` and the explicit diagnostic
  (`QA evidence cannot be consumed by selected provider/model: … Required capability: image input`).
  The configuration is frozen, so resume stays blocked (it would refuse identically); rollback remains available
  and the round, finding and evidence stay in history. Retrying `qa-fix` never opens a second round.

## Delivery suspend (#4, slice 1)

`suspend_v2_work(work_root, reason='')` / `switchyard work suspend <work_root> [reason]` shelves an isolated
delivery worktree as Switchyard-owned data, records `SUSPENDED`, then removes the worktree. It needs no
provider configuration. `resume` restores it (below).

- **Eligible:** isolated work whose operation is `interrupted`, `delivery_done` (e.g. `READY_FOR_MANUAL_QA`,
  `NEEDS_HUMAN`), `waiting_for_human` or `reopened`. Refused: `running` or a live provider
  (`delivery_running`; there is no asynchronous cancellation), `accepted`, `rolled_back`, rollback or
  provisioning phases (`work_not_suspendable: <phase>`), an in-progress QA decision
  (`qa_decision_in_progress`), and legacy in-place work (`legacy_work_not_isolated`).
- **Shelf** `<work_root>/suspend/shelf-<n>/`: `manifest.json` (schema 1) holds `work_id`, `shelf`,
  `repository`, `delivery_branch`, `base_sha`, `head` (delivery branch HEAD at suspend), `files`, `deleted`
  and `index`. `files` describes the COMPLETE Git-visible worktree (tracked plus untracked files not
  `.gitignore`d, as for recovery snapshots and candidates), keyed by repository-relative path:
  `type` (`file`/`symlink`), Git `mode` (`100644`/`100755`/`120000`), `permissions`, `sha256`, `size`,
  `tracked` (in HEAD), and `git_blob` (bytes unchanged from HEAD, kept on the branch) or `blob` (bytes in
  `blobs/<sha256>`, raw, never re-encoded). `deleted` lists HEAD paths absent from the worktree. `index` is
  `{"matches_head": true}` or, if something was staged, every index entry with its own bytes. Symlinks are
  shelved as links. Submodules, in-progress merges/rebases, unmerged or sparse indexes, symlinked parents
  and special files are refused before any write (`unsupported_git_state: …`).
- **Order:** eligibility and representability (no writes) -> operation `suspending` (shelf id, reason, the
  operation being suspended) -> build `.shelf-<n>.<random>` -> verify every byte -> atomic rename to
  `shelf-<n>` -> operation `suspended` (shelf id, path, manifest SHA-256) -> `git worktree remove --force`
  run from the shared Git directory (only while the worktree still equals the shelf) -> operation
  `suspended` with `worktree_removed: true`. The delivery branch and source checkout are never touched.
- **Retry:** `suspend` again finishes whatever is pending: it removes an unpublished build directory, reuses a
  published shelf only if the worktree still equals it (otherwise `worktree_changed_since_shelf`), and
  finishes cleanup. A failed removal leaves `SUSPENDED` with `workspace_cleanup_pending: true`
  (`workspace_cleanup_pending: worktree_remove_failed`). A completed suspend is a no-op.
- **Status:** `terminal: SUSPENDED` (or `SUSPENDING` while incomplete, never `SUSPENDED` without a verified
  shelf), `suspended`, `suspend_reason`, `shelf_id`, `shelf_manifest_sha256`, `workspace_cleanup_pending`,
  `delivery_worktree_state` (`present`/`removed`/`restoring`/`invalid`), `workflow_state` of the suspended
  position, `restore_pending`, `resume_allowed: true`. A worktree removed by suspend is healthy; a missing
  worktree of active work is still `isolation_blocked: delivery_worktree_missing`. `answer`, `qa-fix`,
  `accept` and `rollback` refuse with `work_suspended: resume restores it first`.
- History, budgets, QA rounds, findings, evidence, candidates and human input are untouched; only the
  recovery operation log gains records. Files ignored by `.gitignore` are not product state and are removed
  with the worktree.

### Resume of a suspended work (#4, slice 2)

`resume_v2_work` / `switchyard work resume` on a `SUSPENDED` (or interrupted `RESTORING`) work restores the
same work (same work id, branch, HEAD, worktree path and Git admin directory) before anything else.

- **Verify first, no writes:** the shelf manifest against its recorded SHA-256, every shelf blob, every
  referenced Git blob (`shelf_missing_or_corrupt`, `shelf_manifest_digest_mismatch`, `shelf_blob_corrupt`,
  `shelf_git_blob_missing`), the shelf's provenance against the frozen isolation record, and the delivery
  branch: it must still exist at the shelf's `head` (`delivery_branch_moved`; never reset, rebased or
  transplanted) and descend from `base_sha`. Source/main movement is irrelevant. The recorded path must be
  absent (`delivery_restore_path_conflict`) and the branch not checked out elsewhere
  (`delivery_restore_worktree_conflict`); nothing is deleted. On refusal the work stays `SUSPENDED`.
- **Order:** operation `restoring` -> `git worktree add <recorded path> <delivery branch>` (hooks disabled)
  -> the worktree's Git admin directory is given back its recorded name if Git chose another (recorded
  checkpoints and candidates name it) -> rebuild: `read-tree --reset -u <head>` and `clean -ffdx` in this
  restore-owned worktree, deletions applied, every shelf entry written with its exact bytes, mode and
  permissions (symlinks as links), the index rebuilt from the shelf (staged entries included) -> the worktree
  is re-described exactly as suspend describes it and must equal the shelf (`restore_verification_failed`
  otherwise) -> the suspended operation is re-instated with `restored_shelf`. Until then status is
  `RESTORING`, never a healthy active work.
- **Retry:** a crash anywhere converges: no worktree -> add it; registered worktree on the delivery branch ->
  rebuilt from HEAD + shelf unless it already equals the shelf, which is then reused untouched. A suspended
  worktree whose removal never finished is adopted only if it still equals the shelf.
- **Continuation:** `READY_FOR_MANUAL_QA`, other workflow terminals and human waits are restored only
  (`resume` returns the status; no provider runs, the same human request stays pending). `INTERRUPTED`,
  `REOPENED` and an unfinished qa-fix rejection continue through the ordinary resume rules, including the
  checkpoint drift check: restoring is exact, so only edits made outside the shelf are refused.
- Restoring is control-plane work: it adds no run event, invocation, visit or attempt. Candidates, QA rounds,
  findings and evidence are unchanged; no candidate is published. Shelves are kept; the next suspend creates
  `shelf-<n+1>`. Ignored files are not shelved, so they are not restored either.

## Continuity: adopt prior work (#5, slice 1)

`adopt_v2_work(old_work_root, new_work_root, config_text, provider_runtime_config)` /
`switchyard work adopt <old_work_root> <new_work_root> [--source DIR] [--config FILE]` creates a NEW isolated
work W2 (own work id, `switchyard/<new-work-id>` branch, worktree, budgets and history under the current
configuration) that reuses the durable product of an old work W1. W1 is only read, under its work lock.

- **Target base:** the source checkout (default: W1's) must pass the normal isolation preflight; its HEAD is
  W2's base. It must be W1's `base_sha` (exact reuse) or a descendant of it in the same repository
  (deterministic transplant, below); otherwise `repository_mismatch` / `incompatible_base_lineage`. No
  rebase, cherry-pick or model call.
- **Implementation source** (by W1's durable state): ACCEPTED -> `accepted_commit:<sha>` (tree diff
  base..C from Git); SUSPENDED -> `shelf:<n>` (verified shelf; staging is not carried over); READY_FOR_MANUAL_QA
  with an undecided current candidate and no open round -> `candidate:<n>` (W1's worktree, proven equal to
  the candidate first). Interrupted, reopened, waiting, suspend/restore or QA decision in progress ->
  `implementation_uncertain`; nothing durable -> `no_reusable_implementation`; legacy in-place or pre-v2
  work -> `unsupported_source_schema`. Checkpoints are fingerprints taken mid-run and are never a source.
- **Reused product:** the exact request, W1's verified `sources/` store (never re-fetched), and the latest
  version of every artifact the entry state needs, followed back through their producers (e.g.
  `implementation_handoff`, `microspec`); each must have the same artifact contract in the current
  configuration. W1's candidates, QA rounds, decisions and events are provenance only: W2 starts with no
  candidate and a fresh event log.
- **Entry:** the single command (deterministic) verifier that routes to `READY_FOR_MANUAL_QA`; W2's run
  starts there, so current verification always runs. Adoption itself invokes no provider and consumes no
  budget; VERIFY is ordinary, accounted W2 execution.
- **Order:** W1 read and every byte verified (no writes) -> W2 store: request, all adopted bytes in W2's own
  blobs, operation `provisioning` with the isolation record and the adoption plan -> sources copied ->
  branch at base -> worktree -> rebuilt from base + adopted bytes and verified (paths, bytes, deletions,
  modes, symlinks, permissions where recorded; index = base) -> baseline with `adoption` -> reused artifacts
  -> `running`. A retry converges on one branch, worktree and adoption record; W2's partial worktree is
  rebuilt from base + its own blobs, never from guesses, and W1 is never written.
- **Status:** `adopted`, `adopted_from_work_id`, `adopted_from_base_sha`, `adopted_implementation_source`,
  `adoption_entry_state`, `reused_request`, `reused_specification`, `reused_implementation`,
  `reused_artifacts`, `reused_sources`. The full plan (source identity, changed paths, artifact versions and
  digests, W1's baseline digest) is in W2's baseline `adoption` record.

### Continuity decisions (#5, slice 2)

Adoption writes one durable continuity plan (W2's baseline `adoption.continuity`, schema 1) before W2 runs.
It lists, in workflow order, one decision per product: `stage`, `product`, `action`, `reason` and the stable
identity of the W1 product (`artifact`, `version`, `sha256`, and its `contract`; for the implementation
`source` such as `candidate:2`, `accepted_commit:<sha>` or `shelf:1`, its `identity`, and `digest`, the SHA-256
of the canonical change set). Paths are never identities.

- `REUSED`: W2 consumes W1's durable product; the producing stage is not executed and does not appear in
  W2's event log. `RERUN`: W2 executes it under the current runtime. `INVALIDATED`: W1 has the product, but a
  deterministic rule refuses it.
- **Rules** (explicit, no graph engine): `REQUEST` reused exactly; `SOURCE_RESOLUTION` reused when W1 froze
  sources (absent otherwise); each producing stage on the verifier's input chain (the verifier's inputs,
  then their producers' inputs) is `REUSED` while its latest artifacts exist with an unchanged artifact
  contract, `INVALIDATED: artifact_contract_changed` otherwise, and every later stage on the chain is
  `INVALIDATED: upstream_invalidated: <stage>`. The stage producing the verifier's input carries the
  implementation. The verifier is always `RERUN: current_runtime_verification_required`, listing W1's
  verification only as `historical`: old verification is evidence, new verification is truth.
- **Invalidation blocks in this slice:** the current workflows cannot regenerate an implementation without
  rerunning the expensive stages adoption exists to avoid, so any invalidation refuses with
  `ADOPTION_BLOCKED: continuity_invalidated: <stage>` and nothing of W2 is written; the error carries the
  full plan (`continuity_plan`, printed by the CLI). Missing products keep their own refusals
  (`no_reusable_implementation`, `incompatible_base_lineage`) and produce no plan.
- **Determinism and retry:** decisions depend only on durable identities and contracts. A retry recomputes the
  plan from W1 and refuses `continuity_plan_differs` rather than overwriting; execution never edits it.
- **Read:** `continuity_plan(work_root)` returns the full plan (None for ordinary work); `recovery_status` adds
  `continuity: {reused, rerun, invalidated}` with stage names.

### Changed-base transplant (#5, slice 3)

When the target base B (the clean source checkout's HEAD) is not W1's base A, adoption transplants W1's
implementation deterministically instead of refusing.

- **Preconditions:** same repository (`repository_mismatch`), A an ancestor of B (`incompatible_base_lineage`;
  nothing is fetched), the continuity plan valid (contract invalidation blocks first), the target clean.
- **Algorithm:** the implementation's canonical change set relative to A (the same change set and digest as
  same-base adoption, from the accepted commit, shelf or proven candidate) is committed on A with a fixed
  identity (deterministic objects, no ref), then Git's own 3-way merge runs without a worktree:
  `git merge-tree --write-tree --merge-base=A B I`. The result depends only on A, B and I. No model, no
  `ours`/`theirs` choice, no conflict markers kept anywhere.
- **Clean:** the merged tree becomes a change set relative to B, stored in W2's blobs before any Git step. W2's
  branch starts at B, its baseline records `base_sha = B`, and its acceptance is `B -> C`. The plan's
  IMPLEMENT decision stays `REUSED` (the implementation was not regenerated) with reason
  `deterministic_three_way_transplant` and a `transplant` record: `strategy`, `from_base`, `to_base`,
  `source_tree`, `result_tree`, `result_digest`, `result_paths`, next to the unchanged source
  `implementation` identity and digest. The result is not claimed byte-identical to W1: B's unrelated changes
  are part of it. The plan adds `target_base_sha`; VERIFY is RERUN, because a clean merge proves only the
  absence of textual and structural conflicts. A verifier failure is ordinary W2 workflow.
- **Conflict:** `ADOPTION_BLOCKED: transplant_conflict` listing the paths; nothing of W2 is written. The error
  carries `transplant` (bases, implementation identity, `conflicts: [{path, kind}]`) and the plan with
  IMPLEMENT `INVALIDATED: transplant_conflict`; the CLI prints both. Kinds are Git's, normalized:
  `content`, `binary`, `add/add`, `modify/delete` (implementation modified, target deleted),
  `delete/modify` (implementation deleted, target modified), `file/directory`, `distinct types`, and any other
  `CONFLICT (...)` Git reports. Binaries changed on both sides always conflict; non-conflicting binary
  additions/deletions, mode-only changes and symlinks (as links) transplant exactly.
- **Retry:** the pending adoption keeps its recorded target B even if main moves on; the retry recomputes the
  plan and transplant from W1 against the recorded B and refuses `continuity_plan_differs` if the plan or the
  result differ. Status: `transplanted`, `transplant_from_base_sha`, `transplant_to_base_sha` (and
  `transplanted: false` for same-base adoption).

### Historical sources (#5, slice 4)

Adoption reads older works through a read-only, per-schema reader (`switchyard/versioning.py` names the
schemas; `continuity.READERS` maps each supported one to its provenance and implementation reader). The
reader turns W1 into the canonical inputs planning, transplant and provisioning consume (repository and base,
request, frozen sources, artifact records and contracts, one implementation change set). Nothing is migrated
or written in W1, and W1's event history is never copied.

| Work schema | Produced by | Identified by | Adoptable products |
|---|---|---|---|
| 1 | lifecycle contract 1 works | no `lifecycle_contract_version` (the runtime's own convention) | none: `unsupported_source_schema: work schema 1` |
| 2 | release 2.3.0; `isolation=False` today | contract 2, no `isolation` | request, frozen sources, artifacts with their recorded contracts, and an **accepted commit** only (2.3.0 accepted in place: `C^` must be the baseline HEAD). A candidate's bytes live only in the caller's checkout: `implementation_uncertain` |
| 3 | isolated delivery (current) | contract 2 with `isolation` | everything in slice 1: accepted commit, shelf, proven candidate |

- New works record `work_schema_version` and `runtime_version` (the package version) in their baseline. Works
  before this slice record neither: their schema comes from structure and their runtime version is reported
  as unrecorded (`null`). An explicit version that contradicts the structure, a malformed field or missing
  snapshot/flow/config is `ambiguous_source_schema`; a schema or lifecycle contract newer than this runtime is
  `source_schema_too_new`. Unknown data is never treated as the latest schema.
- A runtime version difference alone invalidates nothing: the same rules apply (contracts recorded in W1's
  artifact catalog are compared with the current ones; a missing contract blocks), IMPLEMENT is REUSED and
  VERIFY RERUN. Same-base reuse and changed-base transplant behave exactly as for a current source.
- W2's adoption record keeps `source` (`work_id`, `work_schema_version`, `runtime_version`, `reader`,
  `baseline_sha256`, `product_digest`: the SHA-256 of the canonical product) and `target`
  (`work_schema_version`, `runtime_version`); status shows `source_runtime_version`,
  `source_work_schema_version`, `target_runtime_version`, `target_work_schema_version`. A retry
  re-canonicalizes W1 and refuses `source_product_changed` if the product digest differs.
- Reading W1 takes its work lock only if the lock file already exists, so adoption creates nothing in W1.

## Transplant: move preserved work onto a new base (#8)

`transplant_v2_work(old_work_root, new_work_root, config_text, provider_runtime_config)` /
`switchyard work transplant <old_work_root> <new_work_root> [--source DIR] [--config FILE]` is the explicit
intent to move W1's preserved implementation from its base A onto the target checkout's HEAD B. It is the
same operation as changed-base adoption (one function, `continuity._derive`): the same per-schema readers and
source rules, continuity plan, `git merge-tree` 3-way engine, W2 blobs, provisioning, materialization,
verification and retry. Only the intent differs:

- **adopt** reuses W1's durable product on whatever base the target has (same base, or a transplant when it
  descends from A). **transplant** requires A != B and refuses a same base before anything is written:
  `TRANSPLANT_BLOCKED: transplant_not_required: same_base` (use `work adopt`, or `work resume` for a
  suspended W1). Same-base suspend/resume is unaffected.
- **Surface:** every engine refusal is reported as `TRANSPLANT_BLOCKED` with the engine's reason, paths and
  structured diagnostics unchanged (`no_reusable_implementation`, `implementation_uncertain: ...`,
  `repository_mismatch`, `incompatible_base_lineage`, `transplant_conflict`, ...). Isolation preflight
  refusals keep their own terminal (`DELIVERY_BLOCKED: dirty_source_worktree`). A conflict carries the same
  `transplant` result as adoption -- `source_work_id`, `from_base`, `to_base`, `strategy`, `implementation`
  (source, identity, digest), `conflicts: [{path, kind}]` -- and the plan with IMPLEMENT `INVALIDATED`; the
  CLI prints both and exits 2. Nothing of W2 except its lock file exists; no resolution is attempted.
- **Sources:** any work the readers prove (accepted commit, verified shelf, proven QA-ready candidate;
  supported historical schemas), including a work that was itself adopted or transplanted. A SUSPENDED W1
  is read from its shelf and stays SUSPENDED with no worktree.
- **Provenance:** W2's adoption record gains `intent` (`adopt` | `transplant`; absent on works adopted before
  #8, meaning `adopt`) next to the existing facts: `from_work_id`/`from_work_root` (the immediate source only),
  `from_baseline_sha256` and `source.product_digest` (source identity), `implementation_identity`, the
  plan's implementation `digest`, and `transplant` (`from_base`, `to_base`, `result_digest`, ...).
  `continuity.derivation_chain(work)` walks the immediate-source links; a new work already in its source's
  chain is refused (`derivation_cycle`).
- **Default `--source`** (adopt and transplant): `continuity.default_source(W1)`, read from W1's frozen
  baseline only (`isolation.source_worktree`; schema 2: the recorded target). W1's live status is never
  consulted, because it may reconcile (e.g. mark a dead RUNNING work INTERRUPTED) and so write into W1.
- **Retry:** a pending derivation is finished only by the same intent (`derivation_in_progress: retry the same
  transplant`); status of a pending one is `transplant_incomplete: retry the same transplant`. The recorded
  target B is kept even if the target branch moves on.
- **Status** (both intents): `derived`, `derivation`, `derived_from_work_id`, plus for a transplant
  `transplant_strategy` and `transplant_result_digest` next to `transplanted`, `transplant_from_base_sha`,
  `transplant_to_base_sha`. The `adopted*` fields are unchanged.

## Integration readiness (#9, slice 1)

`integration_readiness(work_root, target_ref=None)` / `switchyard work integration-status <work_root>
[--target REF] [--json]` answers, without merging anything, whether an accepted delivery commit C integrates
into a target. Acceptance is unchanged and never looks at the target; nothing here merges, rebases, updates a
ref, fetches, pushes or opens a PR (later slices).

- **Eligible:** an isolated work whose operation is `accepted`. Anything else (QA-ready, suspended,
  interrupted, reopened, in-place work) is `INTEGRATION_BLOCKED: work_not_accepted: <state>`. The work's state
  is read directly, never reconciled.
- **C is re-proven** before any assessment: the acceptance record agrees with the isolation record, the QA
  decision names C, C exists, has exactly one parent (the accepted candidate's base head, which descends from
  A), carries this work's acceptance message, and changes exactly the candidate's paths to its bytes and modes.
  Otherwise `INTEGRATION_BLOCKED: accepted_commit_invalid: <detail>`; nothing is repaired. The delivery
  branch is only observed (`delivery_branch_state`: `at_accepted`, `advanced`, `behind`, `diverged`,
  `missing`); C, not the branch head, is the product.
- **Target:** a local branch, ref or commit (default: the source branch frozen in the isolation record at
  delivery start, e.g. `refs/heads/main`), resolved once to T (`target_ref`, `target_sha`). Unknown or
  option-like names: `INTEGRATION_BLOCKED: target_not_found`. No remote is fetched.
- **Classification** (A = delivery base, the work's own `base_sha` -- a transplanted or adopted work uses its
  actual base, never its provenance):

  | Relationship | `readiness` | `drift` | `strategy` |
  |---|---|---|---|
  | T == A | `READY` | `none` | `fast_forward` (result tree = C's tree) |
  | C ancestor of T | `ALREADY_INTEGRATED` | `integrated` | -- |
  | A ancestor of T, clean 3-way | `READY_WITH_DRIFT` | `clean` | `three_way` (result tree) |
  | A ancestor of T, conflicts | `BLOCKED_CONFLICT` | `conflicting` | `three_way` (`conflicts`) |
  | otherwise | `BLOCKED_LINEAGE` | `unsupported_lineage` | -- |

  The 3-way check is the transplant engine itself (`continuity.three_way`: BASE A, OURS T, THEIRS C, `git
  merge-tree --write-tree`), with the same `{path, kind}` normalization, so transplant and integration report
  identical conflicts for the same change. No worktree, index, ref, conflict marker, provider or model; the
  only Git side effect is unreferenced objects in the object store.
- **Repository-level:** the source checkout's branch, dirt or position are irrelevant; the delivery-start
  dirty-source preflight is not applied.
- **Durable, immutable assessments:** each distinct result is a new version of `integration_assessment` in the
  work's own `integration/` store (never the recovery store or operation history). Its `assessment_id` is the
  SHA-256 of the canonical inputs and outcome (durable work id, C, A, target ref, T, readiness, drift, strategy, result
  tree, conflicts); no timestamps. An unchanged re-assessment reproduces the latest id and writes nothing.
- **Freshness:** `integration_status(work_root)` (and `recovery_status()['integration']` for accepted work:
  `target_ref`, `target_sha`, `readiness`, `drift`, `conflicts` count, `current_target_sha`, `stale`,
  `assessment_id`) re-resolves the target ref; `stale: true` when it no longer names the assessed T. Every
  explicit `integration-status` call re-assesses against the current T.
- **CLI:** human summary by default (`--json` for the structured assessment); exit 0 for `READY` /
  `READY_WITH_DRIFT`, 1 for any other readiness, 2 for `INTEGRATION_BLOCKED`.

## Integrate: compare-and-swap integration (#9, slice 2)

`integrate_v2_work(work_root, target_ref=None)` / `switchyard work integrate <work_root> [--target BRANCH]
[--json]` is the first mutation: it moves a local target branch to the accepted delivery by consuming the
work's **latest** readiness assessment. It never reassesses, rebases, squashes, pushes, fetches or invokes a
provider; the delivery branch and C are never moved, and delivery stays `ACCEPTED` (integration is its own
lifecycle in `recovery_status()['integration']`).

- **Gate:** the work is accepted and C re-proven; the target is a local branch (`target_not_found`,
  `target_not_a_branch`); the latest assessment exists (`assessment_missing`), is intact (its id recomputes),
  names the same branch (`assessment_target_mismatch`; `main` and `refs/heads/main` are the same branch) and
  the same work, C and A (`assessment_product_changed`), and the branch still points at its T
  (`target_moved`, with `expected_target_sha`/`actual_target_sha`; reassess with `integration-status`). Only
  `READY` and `READY_WITH_DRIFT` integrate (`assessment_not_ready: <readiness>`).
- **Checked-out targets are refused** (`target_checked_out` with the worktree paths), including the source
  checkout: a ref is never moved behind a checkout. A dirty checkout of another branch is irrelevant.
- **Policy** (C keeps its identity): `READY` -> fast-forward A -> C (`strategy: fast_forward`, no new commit);
  `READY_WITH_DRIFT` -> one merge commit M (`strategy: merge`) with parents T, C and exactly the assessment's
  `result_tree` (never recomputed). M is written as raw commit bytes with a fixed identity and epoch
  (`Switchyard <switchyard@invalid> 0 +0000`) and the message `switchyard: integrate work <id> into <ref>`
  plus `Switchyard-Work` (the durable work id from the delivery branch, not the directory name),
  `Switchyard-Accepted-Commit` and `Switchyard-Assessment` trailers, so the same inputs always give the same M.
  `ALREADY_INTEGRATED` creates nothing and is recorded `integrated_by: already_present`
  (`already_integrated_at`).
- **Compare-and-swap:** `git update-ref <branch> <new> <T>` (hooks disabled). A lost race records `aborted`
  and refuses `target_moved`; nothing is retried or reset.
- **Durable order:** M object -> `integration_record` version `intent` (assessment id and version, expected
  T, C, A, strategy, result tree, planned commit) -> CAS -> `integrated` record (`target_before_sha`,
  `target_after_sha`, `integrated_commit_sha`, `integrated_by: switchyard`, `consumed_assessment_id`). The
  assessment itself is never rewritten. A retry with a pending intent recomputes M from the intent alone and
  requires it to be the recorded M: target still T -> the same M is reused and the CAS retried; target == M ->
  finalized without a second commit; anything else -> `integration_reconciliation_ambiguous` (target never
  reset; status `INTEGRATION_PENDING`). After success, `integrate` is idempotent for that branch.
- **Status:** `integration: {state: INTEGRATED, integrated_by, target_ref, target_before_sha,
  integrated_commit_sha, strategy, consumed_assessment_id, current_target_sha, target_contains_integration}`;
  normal later target movement keeps this provenance. Before integration: `state: ASSESSED` with the
  assessment summary (and `last_integration_attempt` after an aborted one).
