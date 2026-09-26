# Delivery workflow presets

These Python presets are consumers of Switchyard, not runtime features. They are
instantiated once per run with explicit local executable paths. Human QA happens
outside Switchyard after a run reaches `READY_FOR_MANUAL_QA`.

## Workflows

```text
deliver:
ANALYZE -> IMPLEMENT -> VERIFY --success--> READY_FOR_MANUAL_QA
                         |
                       failed
                         v
                    REPAIR_ONCE -> VERIFY_AGAIN --success--> READY_FOR_MANUAL_QA
                                        |
                                      failed
                                        v
                                NEEDS_REDIAGNOSIS

repair:
FRESH_DIAGNOSIS -> FIX -> VERIFY --success--> READY_FOR_MANUAL_QA
                           |
                         failed
                           v
                      REPAIR_ONCE -> VERIFY_AGAIN --success--> READY_FOR_MANUAL_QA
                                          |
                                        failed
                                          v
                                     NEEDS_HUMAN

accept:
CHECK_WORKTREE -> CHECK_SCOPE -> COMMIT -> ACCEPTED
      |               |           |
      +---------------+-----------+-- any unsuccessful outcome --> COMMIT_BLOCKED
```

In deliver, every other unsuccessful outcome (`failed`, `error`, `timeout`,
`cancelled`) goes to `NEEDS_REDIAGNOSIS`. In repair it goes to `NEEDS_HUMAN`.
Only the first deterministic verifier's `failed` outcome schedules a correction;
verifier infrastructure errors/timeouts go directly to the human terminal.
Both verification states use the same configured command and arguments.

The graphs are acyclic, with distinct first and second verification states.
`REPAIR_ONCE` also has `max_state_visits_per_run=1`; there is no second repair edge.
The application invokes the adapter once per agent state and implements no retry.
This bounds runtime-scheduled invocations, not provider-internal tools or turns.
Existing direct-process adapter timeout/descendant limitations still apply.

## Agent stages and artifacts

All agent stages use the existing Codex adapter. It already consumes bounded
verifier diagnostics, whereas the current Claude adapter rejects diagnostics.
The application wraps the original task with stage instructions, the exact
verification argv, and a specific analysis path. It passes existing diagnostic
objects unchanged to Codex. No core request type, adapter or provider abstraction
is changed. Existing Codex approval/sandbox settings apply.

Each new `build_change(...)` creates a unique directory:

```text
<workspace>/.switchyard/delivery/<session>/analysis.md
```

ANALYZE is a separate invocation and must write a nonempty UTF-8 artifact covering
observed/current behavior, constraints, proposed change, affected areas, risks and
verification. FRESH_DIAGNOSIS additionally requires reproduction evidence, actual
execution/data/control flow, root cause, and incorrect prior assumptions. It
explicitly treats previous conclusions and “do not change” assumptions as advisory.
Diagnosis is instructed to avoid production edits; this is an agent instruction,
not a separate filesystem sandbox.

IMPLEMENT/FIX reads the new artifact and reconciles it with current repository
reality. It does not blindly trust the analysis. If an agent cannot safely proceed,
it writes `blocked.txt` alongside the artifact; the application reports `failed`
and the flow chooses the human terminal. This is a refusal signal, not acceptance
or success evidence. Missing/empty analysis also prevents implementation.

The verifier captures up to 4096 diagnostic bytes, using existing capture semantics.
REPAIR_ONCE receives those facts through the existing bounded handoff. Agent prose
never establishes correctness: only the deterministic verifier can reach QA-ready.
Human manual QA is still required after that terminal.

Artifacts are execution context, not permanent product documentation. Before a
real run, add `.switchyard/` to the project's local `.git/info/exclude` (or an
appropriate existing ignore rule). Inspect retained artifacts when diagnosing a
failed run; cleanup is manual. Never reuse one instantiated preset for another
run; instantiate again for a fresh analysis directory.

## Running in a project

From the Switchyard repository, with its requirements installed, for example:

```sh
python3 -m examples.delivery deliver \
  --workspace /absolute/path/to/project \
  --codex-executable /absolute/path/to/codex \
  --verifier-executable /absolute/path/to/project/.venv/bin/python \
  --verifier-arg=-m --verifier-arg=unittest --verifier-arg=discover \
  --verifier-arg=-s --verifier-arg=tests \
  --task "Describe the requested change and scope" \
  --correlation-id change-123 \
  --events-file /absolute/path/to/project/.switchyard/deliver.jsonl
```

Use `repair` with the same executable configuration, a task describing the bug and
reproduction evidence, the same `--correlation-id`, and `--parent-run-id` from the
preceding completed run. Repair always begins with a fresh diagnosis, even if an
older analysis exists. Human QA determines whether to start another repair run
or an accept run. A failed automatic second verification requires new diagnosis,
not another hidden repair inside that run.

Workspace, Codex, verifier and Git executable paths must be absolute. No PATH
lookup, shell expansion or environment interpolation is added. Repeated
`--verifier-arg` values are literal argv; use `--verifier-arg=-m` for leading dashes.
Agent and verifier timeouts default to 300 and 120 seconds and can be configured.
Choose the repository's actual deterministic acceptance checks; this preset does
not invent a test suite or assume that every project uses Python.

The existing YAML schema cannot describe stage-specific task shaping. Rather
than change FLOW-001, these presets use the existing Python Flow/Executor API.
`build_change(...)` and `build_accept(...)` return a `LoadedFlow` with `.flow` and
`.executors`, usable directly with `runtime.run`. They are not YAML executor types
and are not accepted by the generic CLI's `run FLOW_FILE` parser.

The application runner accepts `--events-file`, `--correlation-id` and
`--parent-run-id`, delegating to the existing recorder, context and runtime.
Relative event paths use the caller's cwd; parent directories must already exist.
For accept, keep the event log ignored or outside the target workspace so its
creation does not itself introduce an untracked file. Recording, timing,
RunSummary and `project_runs()` work normally. Context remains
outside executor requests. App exit codes: 0 for QA-ready/accepted, 1 for a human
terminal, 2 for invalid configuration/inspection, 3 for runtime failure. These
application exit policies do not change the generic Switchyard CLI.

## Human QA and the commit boundary

Acceptance is an explicit external human action after successful manual QA.
It does not run an agent, repeat QA, or automatically stage files. Use an isolated
worktree/change: the initial version blocks any unstaged tracked edits and any
nonignored untracked files, even if unrelated files could potentially be separated.
Manual patch staging is possible before acceptance, but leftover mixed work must
be moved to another worktree or otherwise safely separated by the user first.
No automatic hunk attribution, stash, reset, clean or broad `git add` is performed.

1. Review the work and complete manual QA.
2. Stage only the accepted change using your normal Git review workflow.
3. Review the **entire** staged patch (`git diff --cached --binary`). Ensure every
   hunk belongs to this logical change; a file allowlist is not sufficient.
4. Run inspection to obtain the exact HEAD and canonical patch digest:

```sh
python3 -m examples.delivery inspect \
  --workspace /absolute/path/to/project --git-executable /absolute/path/to/git
```

Inspection outputs `base_commit`, `patch_sha256`, `tree` and `branch`. It may
materialize a Git tree object, but does not change refs, stage files, or approve
anything. The SHA-256 covers Git's complete binary/full-index tree diff with
external diff/textconv, renames and color disabled. Use the digest from this
inspection, not a hash of a differently formatted patch command.

5. If that inspected patch is exactly what the human approved, start accept:

```sh
python3 -m examples.delivery accept \
  --workspace /absolute/path/to/project --git-executable /absolute/path/to/git \
  --approved-head FULL_BASE_COMMIT --approved-patch-sha256 REVIEWED_PATCH_SHA256 \
  --message "Describe the accepted change" --manual-qa-passed \
  --task "Human QA passed; the complete staged patch was reviewed" \
  --correlation-id change-123 --parent-run-id LATEST_QA_READY_RUN_ID \
  --events-file /absolute/path/to/project/.switchyard/accept.jsonl
```

The flags are a human scope attestation, not cryptographic proof of who supplied
them. Neither an agent's success message nor automatic digest generation is human
approval. Git cannot infer whether a reviewed patch was already semantically mixed;
that remains the human review responsibility.

CHECK_WORKTREE requires a repository-root workspace on a born local branch, no
merge/rebase/cherry-pick/revert/bisect in progress, no conflicts or submodules,
no sparse/assume-unchanged index entries, and a nonempty staged change. Generated `.switchyard/` artifacts cannot be staged.
CHECK_SCOPE requires the same snapshot and exact approved HEAD/digest. Added hunks
in the same file, extra staged files, or changed base history block acceptance.
COMMIT rechecks scope and commits the captured immutable tree with `git commit-tree`,
then advances the captured branch with `git update-ref` compare-and-swap.

Commit identity comes from the project's Git configuration. This minimal plumbing
path does **not** execute commit hooks; required checks must be in verification
before QA/approval. Keep exclusive access to the worktree while accepting. The
immutable tree prevents concurrently staged changes from entering the commit, and
compare-and-swap prevents overwriting a changed branch. A failed ref update can
leave an unreferenced commit object; existing history is preserved. The index is
never reset or replaced. Nothing is pushed. Remote publication is separate.

A parent run may be in another log. The same correlation ID and parent references
record provenance only; no runtime rule checks a delivery lifecycle or calculates
lifecycle duration. Delivery analytics remain deferred to OBS-006.

## Offline verification

Run `.venv/bin/python -m unittest discover -s tests -p 'test_delivery*.py'` from
the Switchyard repository. On 2026-09-26, all 18 focused tests passed. The full
offline suite ran 206 tests: 205 passed and one existing Windows-only skip.
Tests use controlled Codex boundaries, real deterministic verifier subprocesses,
and disposable Git repositories; they make no provider calls. No generic
Switchyard contract or implementation changed in DELIVERY-001.

## OBS-006 delivery analytics Python API

The application-specific consumer in `examples.delivery.analytics` recognizes
DELIVERY-001 history shapes without changing runtime or generic observability:

```python
from switchyard.observability import read_events
from examples.delivery.analytics import (
    summarize_delivery_analytics, summarize_delivery_lifecycles,
)

events = tuple(read_events("events.jsonl"))  # read once; iterators are single-use
report = summarize_delivery_analytics(events)
for lifecycle in report.lifecycles:
    print(lifecycle.correlation_id, lifecycle.run_ids, lifecycle.final_terminal)
    print(lifecycle.accepted, lifecycle.incomplete, lifecycle.automation_duration_ns)

rate = report.first_pass_verification_rate
print(rate.numerator, rate.denominator, rate.unknown_count, rate.value)
# Or request just the lifecycle tuple:
assert summarize_delivery_lifecycles(events) == report.lifecycles
```

Both functions accept canonical Event iterables, including `read_events()` output,
and never open files themselves. They use public `project_runs()` and
`summarize_runs()` for generic validation, grouping, counts, timing and provenance.
Their result dataclasses, nested run summaries, identifier tuples and ratio values
are immutable. `docs/plans/obs-006.yaml` retains the observability sequence name;
all delivery interpretation stays in the application directory.

### Classification and lifecycle membership

The first actual `state_entered` identifies a candidate: `ANALYZE` means deliver,
`FRESH_DIAGNOSIS` repair, and `CHECK_WORKTREE` accept. Observed states, transitions,
outcome targets and terminals must match the corresponding DELIVERY-001 graph or
a valid prefix of it. A terminal name alone never establishes a workflow type.
Unrelated or incompatible shapes, and `run_started`-only prefixes, appear in
`report.unrecognized_run_ids`. This is shape recognition, not authenticated proof
of which executor/provider or preset generator produced the events.

Recognized runs appear individually in `report.runs` as `DeliveryRunSummary`,
with `.workflow`, the unchanged generic `.run` RunSummary, `.ended`,
`.verification_eligible`, and optional `.first_pass_verification`,
`.automatic_repair_used`, and `.repair_success` facts.

Non-null correlation IDs group recognized runs into separate lifecycle summaries.
Uncorrelated recognized runs remain in run-level metrics and are explicitly listed
in `uncorrelated_run_ids`/`uncorrelated_run_count`; they contribute no lifecycle
metrics and are never grouped under a fabricated ID. Lifecycle groups retain
first-seen order. Within a group, explicit parent edges take precedence; ties use
first-seen order. Wall clocks are not used to override provenance.

`DeliveryLifecycleSummary` exposes individual `run_ids`, deliver/repair/accept run
counts, final terminal, acceptance, QA decision/reopen/pass counts, automatic
repair count, separate rediagnosis/needs-human counts, total executor invocations,
observed automation duration and an incomplete flag.

### Exact metrics

Each aggregate `*_rate` is a `Ratio` with integer `numerator`, `denominator` and
`unknown_count`. `.value` is an exact `fractions.Fraction` when the denominator is
positive and no eligible outcome is unresolved. Otherwise it is `None`, including
zero denominators. Counts remain available even when a complete rate is unknown.
The consumer does not silently turn unfinished runs into negative observations.

| Metric field | Numerator | Denominator |
| --- | --- | --- |
| `first_pass_verification_rate` | First VERIFY succeeds, run finishes QA-ready, and REPAIR_ONCE was not entered | Deliver/repair runs actually entering VERIFY |
| `automatic_repair_rate` | Actual REPAIR_ONCE entries | Deliver/repair runs actually entering VERIFY |
| `automatic_repair_success_rate` | VERIFY_AGAIN succeeds and run finishes QA-ready | Runs actually entering REPAIR_ONCE |
| `rediagnosis_rate` | Deliver runs ending NEEDS_REDIAGNOSIS | Recognized deliver runs |
| `needs_human_rate` | Repair runs ending NEEDS_HUMAN | Recognized repair runs |
| `manual_qa_reopen_rate` | QA-ready parent runs with an observed repair child | QA-ready parent runs with an observed repair or accept child in the same correlation |
| `manual_qa_pass_rate` | QA-ready parent runs with an observed accept child | Same observed-decision cohort |
| `commit_blocked_rate` | Accept runs ending COMMIT_BLOCKED | Recognized accept runs |

Reaching VERIFY means actual state entry, not a transition selecting it. A run
ending before VERIFY is ineligible for the first two ratios. A recorded failed
VERIFY already disproves first-pass verification; it does not establish repair
use until REPAIR_ONCE is entered. A successful VERIFY without the required
QA-ready completion is still pending. An unfinished corrective attempt has unknown
success unless a non-success second verification already disproves it. A
`run_error` is an observed end, not a missing future completion.

Manual QA is inferred only from the documented external command convention:
a QA-ready parent followed by a recognized repair or accept child. The child need
only start its initial state; it need not finish. Automatic REPAIR_ONCE is never
a manual reopen. A QA-ready parent with no decision child is pending, excluded
from the QA denominator. Multiple same-kind children count one parent decision;
repair and accept children of the same parent are contradictory decisions.
An accept child records the human-QA-pass convention even if committing is blocked.
The runtime did not itself conduct manual QA.

`accepted` means at least one accept run reached ACCEPTED. READY_FOR_MANUAL_QA and
COMMIT_BLOCKED are not acceptance. This historical acceptance fact survives a later
reopen. `invocations_per_accepted_lifecycle` is a Ratio whose numerator sums all
observed executor invocations in groups with a unique observed final ACCEPTED run,
and whose denominator counts those groups. Its `.value` is also exposed as
`mean_invocations_per_accepted_lifecycle`; the numerator is
`total_invocations_for_accepted_lifecycles`. Groups reopened after acceptance or
with ambiguous final branches are excluded from this mean, even when their
historical `accepted` flag remains true. These are **executor** invocations,
including deterministic commands, not inferred agent/provider invocations.

Count properties on the dataset include `lifecycle_count`,
`accepted_lifecycle_count`, `incomplete_lifecycle_count`,
`eligible_verification_run_count`, `first_pass_verification_count`,
`automatic_repair_count`, `automatic_repair_success_count`, `rediagnosis_count`,
`needs_human_count`, `qa_decision_count`, `manual_qa_reopen_count`,
`manual_qa_pass_count`, `accept_run_count`, and `commit_blocked_count`.

### Partial histories and errors

A lifecycle is incomplete when a recognized run is unfinished, a parent is absent
or cannot be classified/grouped, the available runs are not one linear chain
rooted in a parentless deliver run, or a QA-ready run lacks an observed decision.
Missing external parents are tolerated. Multiple roots/branches are retained;
multiple leaves yield no invented final terminal. Unrecognized records sharing a
correlation also mark its lifecycle incomplete and make the final terminal unknown,
without adding those records to delivery metrics. Explicit failed terminal endings
are resolved outcomes rather than intrinsically incomplete data.

`automation_duration_ns` sums OBS-003 `run_duration_ns` for observed recognized
runs in the lifecycle. If any such duration is unavailable, the sum is `None`.
Missing external history is separately signaled by `incomplete`; the sum never
claims to include unobserved work. Human testing time is excluded. No cross-run
monotonic subtraction, wall-clock duration, provider cost or token estimate is used.

Generic invalid event records/history still raise `ObservabilityError` unchanged.
`DeliveryAnalyticsError` covers contradictory application evidence: different
present non-null parent/child correlation IDs, parent cycles, conflicting QA
choices, accept children of known ended non-QA-ready parents, or multiple workflow
initial-state signatures in one candidate run. An incomplete parent log is not
proof of an invalid relationship. No data is repaired or silently discarded.

There is no analytics CLI, dashboard, file-loading wrapper or generic contract
change in OBS-006.

OBS-006 verification (2026-09-26): 25 focused analytics tests, all 18 DELIVERY-001
regressions, and all 74 OBS-001..005 regressions passed. Full offline suite:
231 tests, 230 passed and one existing Windows-only skip. No real provider calls.
Generic contracts and DELIVERY-001 execution behavior are unchanged.
