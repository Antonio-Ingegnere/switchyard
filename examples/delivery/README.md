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
