# RUNTIME-001 through RUNTIME-007

Minimal synchronous Python control plane using only the standard library.
Run checks from this directory (Python 3.10 or newer):

```sh
python3 -m unittest discover -s tests -v
```

## Files

- `runtime/__init__.py`: immutable data contracts, executor protocol, graph
  validation, state-machine runner, and in-memory events.
- `tests/test_runtime.py`: deterministic fake executor and minimal inline flows
  covering the MicroSpec verification criteria.
- `tests/test_state_visit_budget.py`: state-visit admission and bounded-cycle checks.
- `runtime/command.py`: immutable Command and direct CommandExecutor.
- `tests/test_command.py`: real subprocess tests and deadline/error-boundary checks.
- `runtime/recording.py`: synchronous JSONL recorder.
- `tests/test_recording.py`: persistence, ordering, and single/double failure checks.
- `examples/micro-spec.yaml`: accepted RUNTIME-001 contract.
- `docs/plans/runtime-002.yaml`: accepted state-visit budget contract.

`examples/flow.yaml` is documentation for a future delivery flow. It is not
loaded or executed by the implementation or tests.

## Local implementation choices

- WorkUnit contains only an immutable task string. No metadata schema or context
  selection API is introduced.
- Flows are Python values; serialization and configuration parsing are absent.
  State executor names resolve through a supplied dictionary of Executor objects.
- Execution is synchronous, with one invocation per executable state visit.
- Flow and transition mappings are copied and read-only so ordinary caller
  mutations cannot change the validated graph during execution.
- Terminal states cannot declare executors or outgoing transitions. An initial
  terminal state is valid. Unreachable states are validated too.
- Missing `initial` fails at Flow construction. Invalid references, definitions,
  outcome/event keys, and unbounded cycles fail validation before executor calls.
- ExecutorResult is an exact frozen data record containing only `outcome`.
  Invalid outcomes, extended result classes, and other return values are rejected
  at the invocation boundary. This contract is not a sandbox against malicious
  Python code in the same process.
- Each run gets a UUID-based ID; visit and invocation IDs use separate counters
  qualified by that run ID. Event ordering and transitions are deterministic;
  literal run IDs differ. The runner delegates optional recording through record(event); executors own process I/O.
- Successful runs return a terminal state and an immutable tuple of observed events.
  ExecutionError exposes observed history; execution failures end with run_error,
  while ordinary recorder failures end with the event whose recording failed.
  Validation errors occur before a run starts.

## Accepted executor exception semantics

RUNTIME-001 is accepted and complete. [ADR-007](docs/decisions/007-executor-contract-exceptions.md)
records the accepted distinction: expected operational failures must be returned
as normalized ExecutorResult outcomes; exceptions escaping Executor.run abort
through ExecutionError without ordinary transitions or automatic retry.

## RUNTIME-002 state-visit admission

RUNTIME-002 is accepted and complete.

Each executable state may set `max_state_visits_per_run` to a nonnegative integer
(excluding bool); `None` means no local limit. Counters are local to each run.
The runtime checks admission before creating visit/invocation identities. Refusal
emits `budget_exceeded` with the configured limit and already admitted visit count,
then selects the declared terminal target, or raises ExecutionError if absent.

A selected target does not prove entry. The existing source visit exits after
its `transition_selected` event and before target admission. Refused targets have
no `state_entered`, `state_exited`, visit ID, or invocation ID.

Outcome-driven transition events keep their `outcome` field. Runtime budget
transitions instead set `trigger_event="budget_exceeded"` and leave `outcome`
empty. Budget events add `metric`, `limit`, and `observed`; no executor contract
or generic budget interface is introduced.

Validation removes bounded states and incident outcome edges, then requires the
remaining graph to be acyclic, including unreachable components. Budget edges
must lead to terminals. All 24 unittest checks pass, including the exact repair
sequence and unchanged RUNTIME-001 regression checks.

## RUNTIME-003 command execution

RUNTIME-003 is accepted and complete.

CommandExecutor owns an immutable Command with absolute executable/cwd, literal
argv tuple, and positive finite invocation_timeout_seconds. WorkUnit retains only
its task and is never used to configure argv, stdin, or environment. Launch uses
shell=False, inherited environment, and DEVNULL for all standard streams.

The monotonic deadline starts immediately before Popen. Creation consumes the
allowance; after launch only the remaining time may be waited. Expiration during
creation returns timeout even if the process already exited. A timeout result is
returned only after the direct process has exited and wait completed. A running
process is killed immediately, without a grace period. Cleanup can exceed the
deadline; descendants are not supervised.

Launch OSError normalizes to error. Other implementation/cleanup exceptions escape
through ADR-007. Exit 0 maps to success and other exit codes to failed. All outcome
transitions remain owned by the existing runtime; its code was not changed.

DEVNULL is temporary minimal behavior for this slice. A separate bounded-output
mechanism is needed before repair/AI flows can consume verifier diagnostics.
There is no output capture, persistence, metadata extension, or logging pipeline.

Validation: 40 tests discovered, 39 passed, one Windows-only batch-path test skipped
on the tested macOS host. The previous 24 tests retain their meaning unchanged.
Process-creation deadline tests use real processes and controlled monotonic values
for both still-running and already-exited subprocesses.

## RUNTIME-004 persistent recording

RUNTIME-004 is accepted and complete.

Pass `recorder=JsonlRecorder(path)` to `run` (import from `runtime.recording`).
The runtime first adds each immutable event to its observed in-memory history,
then calls record(event) synchronously before proceeding. Without a recorder,
execution retains its prior behavior.

The recorder serializes all existing Event fields (None becomes null) before
opening the file. Each event uses binary append, full write, explicit flush, and
close. JSON is compact, sorted-key UTF-8 with one LF per object. There is no fsync:
this is persistent logging, not crash-durable storage. A failed write or crash can
leave a partial final line; a failed call may also have written the complete line.
No rollback, retry, repair, or recovery is provided. A failed recorder is unusable.

An ordinary recorder exception is the cause of ExecutionError and prevents all
subsequent execution. Its failed event remains in ExecutionError.events; no extra
run_error is emitted. If recording an existing run_error fails, the original
ExecutionError message/cause remains primary and `recorder_error` holds the
secondary recording exception. This field is None otherwise.

ExecutionError.events is **in-memory observed event history**. JSONL contains the
successfully recorded persistent prefix, possibly followed by an unconfirmed
failed append. **Observed event != confirmed persisted event.**

Validation: 52 tests discovered, 51 passed, one Windows-only test skipped on macOS.
All previous 40 tests are unchanged. New tests cover every event failure position,
real command recording, append ordering, serialization and I/O failures, partial
lines, and primary execution error preservation during run_error write failure.

## RUNTIME-005 Codex executor

RUNTIME-005 is fully accepted following the successful real smoke below.

`runtime/codex.py` provides immutable CodexConfig and CodexExecutor. Configuration
owns the absolute executable/workspace paths and invocation timeout. The executor
constructs a Command and delegates to the unchanged CommandExecutor; nonzero CLI
exit (`failed` at the command layer) becomes technical `error`. Exit 0 means only
technical success. A following deterministic verifier must decide correctness.
Core runner, WorkUnit, Event, and ExecutorResult are unchanged.

The literal argument list is:

```python
[codex_executable, "--ask-for-approval", "never", "exec", "--ephemeral",
 "--ignore-user-config", "--ignore-rules", "--sandbox", "workspace-write",
 "--skip-git-repo-check", "--", "WorkUnit task:\n" + work_unit.task]
```

The existing CODEX_HOME authentication remains an external prerequisite. User
config.toml and user/project exec-policy rules are ignored via CLI flags; AGENTS.md
remains native workspace behavior. This does not claim hermetic execution.
Syntax checked using installed codex-cli 0.153.4 help without a model invocation.

Each invocation starts a fresh process with shell=False, cwd=workspace, inherited
environment, and DEVNULL streams. Workspace must exist and mutations are not
rolled back. Prompt is a literal argv element, subject to OS argument-size limits
and local process visibility. No output is captured or interpreted for acceptance.

Timeout guarantees only that the directly launched process has exited and been
waited for. Descendants may continue running and modifying the workspace after
return. **Timeout is not a complete cancellation boundary for agent side effects**;
this limitation matters for future production usage. Prefer a native Codex binary
rather than a wrapper that starts another process.

Validation: 62 offline tests, 61 passed and one Windows-only skip. The prior 52
tests are unchanged. `tests/test_codex.py` uses a controlled subprocess boundary
that launches only fake Python code and deterministic verifiers, never real Codex.

Manual real smoke command:

```sh
python3 manual_smoke/codex_smoke.py --run-real --codex-executable /absolute/path/to/codex
```

This opts into one real model invocation with a 120-second deadline in a temporary
workspace, followed by an exact-byte file check using CommandExecutor. Existing
local authentication is required. Import and normal unittest discovery never run
it. The script reports technical outcome separately from verifier outcome; PASS
requires terminal accepted. Real smoke executed on 2026-09-12 with codex-cli 0.153.4 at
`/opt/homebrew/bin/codex`, on macOS 26.6.2 arm64 (Mach-O):

- Successful run outside the execution sandbox: CodexExecutor technical outcome
  `success`; deterministic CommandExecutor verifier outcome `success`; terminal
  state `accepted`; smoke `PASS`; script exit code 0.
- The verifier checked exact bytes `runtime-005-ok\n` in `result.txt` in a fresh
  temporary workspace. Agent text was not used as evidence.
- That successful run performed exactly one agent invocation and one verifier
  invocation, with no runtime/executor retry or repair loop.
- An earlier separate smoke attempt inside the execution sandbox returned
  `implement error`, terminal `failed`, smoke `FAIL`, script exit code 1; verifier
  was not invoked. Because CLI output was discarded, the precise technical cause
  is not established. The subsequent outside-sandbox run succeeded without code
  changes. This is an execution-environment/CLI integration observation, not
  evidence of a runtime architecture defect. Two separate smoke attempts occurred;
  neither run contained an automatic retry.

## RUNTIME-006 Claude executor

RUNTIME-006 is fully accepted following the successful real smoke below.

`runtime/claude.py` adds concrete ClaudeConfig and ClaudeExecutor. It composes the
unchanged CommandExecutor and maps nonzero exit to technical error. No core,
CodexExecutor, Event, budget, recorder, or WorkUnit changes were required.

Exact argv (one literal prompt argument):

```python
[claude_executable, "--print", "--no-session-persistence", "--output-format", "text",
 "--restricted", "--permission-mode", "acceptEdits", "--permission-prompts", "none",
 "--tools", "Read,Edit,Write,Glob,Grep", "--disallowedTools", "mcp__*", "--", "WorkUnit task:\n" + work_unit.task]
```

Combined flags checked using Claude Code 2.1.266 --help without a model call.
No safe-mode/setting-sources flags are passed. Restricted mode ignores ordinary
user/project/local settings and constrains file tools to working directories.
Managed settings and inherited environment can still affect provider/model
behavior; execution is not hermetic and runtime does not bypass organizational
policy. No assertion is made about CLAUDE.md discovery; the task does not depend
on that file. Local auth is used naturally, without credential reads or login.

The configured built-in tools are Read/Edit/Write/Glob/Grep. No Bash, Agent,
WebFetch/WebSearch, or MCP integration is added. Because --tools controls only
built-ins, --disallowedTools mcp__* explicitly denies MCP tools. Permission prompts are denied by
the provider, not routed to runtime. Production standard streams remain DEVNULL.
Technical CLI success is distinct from verifier acceptance. Mutations are not
rolled back. Direct-process timeout is not a complete cancellation boundary:
descendants may continue modifying the workspace after timeout returns.

Offline validation: 74 tests, 73 PASS, one Windows-only SKIP. The prior 62 tests
remain unchanged. New tests include exact flags/task, exit mapping, deadlines,
workspace artifacts, misleading text, three concrete executor implementations in
one runtime, fresh processes, and diagnostic-smoke behavior without real AI calls.

Manual Claude smoke command:

```sh
python3 manual_smoke/claude_smoke.py --run-real --claude-executable /opt/homebrew/bin/claude
```

Real smoke executed successfully on 2026-09-12 outside the execution sandbox:

- Claude Code version: **2.1.266**, executable `/opt/homebrew/bin/claude`.
- Platform: **macOS 26.6.2 arm64 (Mach-O)**.
- Policy included `--restricted`, file-only built-in tools, and explicit
  `--disallowedTools mcp__*`.
- Exactly **one agent invocation**, no retry and no diagnostic model call.
- ClaudeExecutor technical outcome: **success**.
- CommandExecutor deterministic verifier outcome: **success**; exact file bytes
  `runtime-006-ok\n` were checked in `result.txt`.
- Terminal state: **accepted**; smoke **PASS**; script exit code **0**.
- Successful temporary workspace was cleaned up by the smoke script.

Full offline suite after the MCP-policy update: **74 tests, 73 PASS, one
Windows-only SKIP, zero failures** (`python3 -m unittest discover -s tests -q`
from `repo/`). SHA-256 comparison confirmed no changes to core runner,
CommandExecutor, CodexExecutor, or recorder.

It uses one agent invocation then an exact-byte CommandExecutor verifier. Failure
prints exact JSON argv and a shell-quoted manual rerun with cwd, retaining the
temporary workspace so the command remains usable. Diagnostic rerun uses terminal
output, is outside acceptance, may consume another model call, and never runs
automatically. Success removes the smoke's temporary workspace. No diagnostics
or output capture were added to runtime.

Actual code comparison: both AI adapters duplicate a small frozen config,
Command-based structural validation, config type check, prompt label, Command
composition, and failed-to-error mapping. Both already reuse process control.
Provider flags, permission/config/auth policies and transcript options differ and
remain concrete. No shared agent base/factory/registry was extracted.

RUNTIME-007 and general provider abstraction are not started.


## RUNTIME-007 bounded diagnostic handoff

The Executor API is intentionally revised to
`run(InvocationRequest) -> InvocationResponse`. Runtime's top-level
`run(work_unit, flow, executors, recorder=...)` is unchanged. Requests carry the
same immutable WorkUnit and optional DiagnosticInput; responses carry the unchanged
outcome-only ExecutorResult and optional DiagnosticPayload. No legacy argument shim.

DiagnosticPayload contains exact valid UTF-8 bytes with a hard 4096-byte output
cap. Constructors and runtime response validation independently check type/size/
encoding. Input provenance (run, state, visit, invocation) is assigned by runtime,
not accepted from producers. Text is untrusted data and never transition policy.

Executable states opt in via `diagnostics_from_previous_max_bytes=0..4096`; None
opts out. Zero allows present empty data only; absence is still represented by None.
Only the immediately preceding invocation's output is eligible. There is no
transitive forwarding, historical fallback, cross-run storage, or diagnostic archive.

Admission checks budget first without committing. Budget refusal drops diagnostics
and follows existing budget_exceeded semantics. Otherwise diagnostic input is
validated before incrementing visits or allocating target IDs. Input overflow
emits run_error with target name but no visit/invocation ID or target entry/exit.
Only successful checks commit admission. Existing recorder failures can still stop
execution at event boundaries; their semantics have not changed.

The whole response is validated before invocation_finished or transition selection.
Invalid producer diagnostics abort as malformed invocation output, with no normal
transition, handoff, or budget event. Overflow rejects; nothing is truncated.

CommandExecutor, CodexExecutor, and ClaudeExecutor were mechanically migrated to
envelopes. Their CLI/process behavior is unchanged for diagnostics=None. They
explicitly reject any diagnostic input before process launch; agent prompting and
real capture are deferred. Manual smoke scripts call the top-level runtime and
needed no migration; no real model smoke was run in this slice.

Event fields and JsonlRecorder are unchanged. Neither payload nor origin is added
to JSONL, RunResult, or error messages. Existing events cannot reconstruct diagnostic
inputs. Runtime's added handoff storage is limited to current input and output;
this does not bound arbitrary executor allocations, traceback retention, total
event history, or task/state size, and is not bounded stdout/stderr capture.

Validation: 87 offline tests, 86 PASS and one Windows-only SKIP. Previous 74 tests
retain equivalent assertions after mechanical envelope migration. New tests cover
exact event sequences, actual uncommitted visit counters on overflow, forged or
invalid producer output, UTF-8/byte limits, fresh provenance in cycles, opt-out and
nontransitive delivery, recorder failures, and unsupported provider input.

RUNTIME-008 and subprocess diagnostic capture are not started.

## RUNTIME-008 bounded command diagnostics

Implemented opt-in `Command(..., diagnostic_capture_max_bytes=B)` for exact int
0..4096. Default None retains DEVNULL and diagnostics=None; provider adapters are
unchanged. Capture merges stderr into stdout, drains nonblocking reads of at most
4096 bytes, and retains the last B raw bytes. Zero explicitly retains nothing.
UTF-8 replacement followed by a complete-code-point suffix limit produces valid
UTF-8 within B; no truncation marker. Every successfully launched normal response
(including timeout) has a present payload when capture is enabled, possibly empty.

Direct-process deadlines still begin before Popen. A latched timeout cannot hide
capture or cleanup failure. Internal capture failures use ADR-007; if cleanup also
fails, the capture exception remains primary with cleanup chained as its cause.
Direct-process cleanup is attempted once; no cleanup retry loop is introduced.
After confirmed direct-process completion, final drain is limited to 256 reads
of at most 4096 bytes, stopping at EOF/would-block, then the endpoint is closed.
No thread or descendant EOF is awaited. Late/unread output can be lost; descendants
can survive and mutate workspace, and their later writes may encounter a broken
pipe. Diagnostics remain in-memory only and never enter Event/JSONL.

Implementation support: Unix Python 3.10+, Windows Python 3.12+ for capture;
older Windows Python rejects explicit capture before launch. Windows read-exception
behavior has NOT been verified by this run. Existing capture-disabled support is
unchanged. See [MicroSpec](docs/plans/runtime-008.yaml) for exact semantics.

Verification on 2026-09-13: macOS 26.6.2 arm64 (Darwin 25.6.0), Python 3.14.7.
`python3 -m unittest discover -s tests -v`: **100 tests, 99 PASS, 1 skip**, 5.199 s.
The skip is existing Windows batch-path validation. No model calls or real provider
smokes were run. All 17 RUNTIME-008 acceptance criteria passed on this platform.

- Real 8 MiB stdout/stderr producer exited 7 without deadlock under an independent
  watchdog. Cap 4096: retained high-water 4096, read high-water 4096 bytes.
  Cap 0: retained high-water 0, read high-water 4096 bytes; empty payload returned.
- Traced retention/normalization allocation peaks: 16,564 bytes for both 1 MiB and
  16 MiB processed. This excludes producer allocations and is not a whole-process
  RSS guarantee. Tests also assert requested read size and private retention bounds.
- Exact ASCII, cap/cap+1, Unicode, invalid bytes, multibyte boundary and read-chunk
  independence tests passed. `Aé` at cap 2 yields `é`, at cap 1 empty; raw A+FF
  at cap 3 yields U+FFFD, at cap 2 empty. Cap 0 regression explicitly covers [-0:].
- Timeout during creation, capture failure overriding that timeout, early EOF,
  continuously writing direct child, and silent/writing descendants after normal
  exit and timeout passed. Final-drain attempt bound and endpoint closure passed.
- Capture+cleanup failure preserves original exception identity as the cause of
  ExecutionError and cleanup as its chained cause; no transition is selected.
- Real command exit 7 -> failed + exact `ERROR` tail -> fake consumer -> accepted.
  WorkUnit identity and runtime-owned originating IDs match; JSONL omits payload.

SHA-256 before/after matched for all protected files (Event is in runtime/__init__.py):

```text
d1cde025d575410ec85bd29303dbcc14940699b065e659d8dfa1cf93106fd1fd  runtime/__init__.py
da0421cc0d4d9a6149eeab4a02b54280da07e495e8b5cfb0618036e5a68e99fd  runtime/codex.py
5ddf25faded6cdaa492472a894db8ec25c505ac52b55278e793d0986b36af4f7  runtime/claude.py
7bb866fba309ab7e824bb905b88aa4eba90d9c930e196b93aae24355f7defeeb  runtime/recording.py
```

Only runtime/command.py, new tests/test_command_capture.py, this README, and
MicroSpec runtime-008.yaml changed for this slice. RUNTIME-009 is not started.

## RUNTIME-009 Codex diagnostic consumption and bounded repair

CodexExecutor now consumes explicit DiagnosticInput in a Codex-specific untrusted
JSON-string prompt section. Absent diagnostics retains the exact old prompt;
present empty diagnostics adds `""`. Origin IDs are not included. The inner
CommandExecutor receives the same WorkUnit with diagnostics=None; its contract,
CLI flags/auth/deadlines and DEVNULL behavior are unchanged. No provider abstraction.

The wrapper is 233 UTF-8 bytes; JSON encoding of 4096 NUL bytes is exactly 24,578
bytes, yielding exactly 24,811 added bytes. The test also launches the fake CLI
with that worst-case prompt. Task size, token count and OS argv limits are separate.

Offline verification (2026-09-13): macOS 26.6.2 arm64, Python 3.14.7;
`python3 -m unittest discover -s tests -v`: 105 tests, **104 PASS, 1 skip**, 6.231 s.
Skip: existing Windows batch-path validation. All offline RUNTIME-009 criteria
passed; no real model calls were made during that offline verification.
Subsequently the user reported real repair smoke PASS and accepted RUNTIME-009
fully. This records the user report; FLOW-001 does not rerun that smoke.
Exact byte capture/handoff/prompt checks use the manual verifier's literal bytes
without a newline. Both accepted and failed-repair event sequences are asserted,
including transition targets, budget refusal and exactly one scheduled repair.
Existing Codex tests are unchanged; only its now-obsolete diagnostic-rejection
case was removed from the shared rejection test (Command/Claude cases remain).

Manual real smoke (explicit opt-in, may consume provider tokens):

```sh
python3 manual_smoke/codex_repair_smoke.py --run-real --codex-executable /absolute/path/to/codex
```

It starts from `result.txt = b"broken\n"`, runs real Command verification, then at
most one runtime-scheduled Codex CLI repair invocation, then verifies again.
PASS requires verify:failed -> fix:success -> verify:success -> accepted and exactly
one started fix invocation. Otherwise FAIL; a second verifier failure causes
budget_exceeded -> escalated without a second scheduled Codex repair invocation.
FAIL retains workspace and prints its path plus exact diagnostic argv/cwd. PASS
removes workspace. Manual rerun is separate, never automatic, and outside acceptance.
The smoke lifecycle itself was tested offline with fake CLI processes for PASS/FAIL.

**One outer Codex invocation is not one model call.** Provider-internal model turns,
tool iterations, token consumption and cost are not bounded by this state-visit
limit. Existing descendant/cancellation limitations remain unchanged.

Protected SHA-256 before/after matched (Event lives in runtime/__init__.py):

```text
d1cde025d575410ec85bd29303dbcc14940699b065e659d8dfa1cf93106fd1fd  runtime/__init__.py
a637e5d59ab112be7f23a50ebd9c73b30c01e28f6187be6d7a8da733451e3d54  runtime/command.py
5ddf25faded6cdaa492472a894db8ec25c505ac52b55278e793d0986b36af4f7  runtime/claude.py
7bb866fba309ab7e824bb905b88aa4eba90d9c930e196b93aae24355f7defeeb  runtime/recording.py
```

Changed: runtime/codex.py, tests/test_diagnostics.py, new tests/test_codex_repair.py,
new manual_smoke/codex_repair_smoke.py, README and docs/plans/runtime-009.yaml.
Claude diagnostic consumption and the next slice have not started.

## FLOW-001 declarative configuration frontend

`flow_config.py` loads YAML into the existing immutable Flow/State and concrete
Command/Codex/Claude executors. Runtime remains YAML-independent. FLOW-002/CLI
has not started. RUNTIME-001..009 foundation is accepted, including the user-reported
RUNTIME-009 real smoke PASS.

The first deliberate external dependency is `PyYAML>=6.0.3,<7`, required only by
this frontend. Install into your environment before running the complete suite:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Library API (run from this directory):

```python
from flow_config import load_flow
from runtime import WorkUnit, run

loaded = load_flow('/absolute/project/flow.yaml')
result = run(WorkUnit('Your task'), loaded.flow, loaded.executors)
```

Loading never executes a command or probes authentication. LoadedFlow is frozen;
its executor mapping is copied and read-only. `task` is not a YAML field.
Missing PyYAML fails inside load_flow with FlowConfigError and installation guidance;
importing flow_config/runtime itself does not require importing yaml.

Schema v1 requires exactly version: 1, initial, executors and states. Command
requires type, absolute executable, argv list and positive finite timeout_seconds;
workspace defaults to '.', diagnostic_capture_max_bytes defaults to null and allows
exact integers 0..4096. Codex/Claude require type, absolute executable and timeout;
workspace defaults to '.'. Provider-specific flags/environment are not configurable.

Terminal state has exactly `{terminal: true}`. Executable state requires executor
and on, may have max_state_visits_per_run / diagnostics_from_previous_max_bytes,
and MUST omit terminal (including terminal: false). Budget/diagnostic null means
None; bool is not an integer limit. Unknown fields and references are rejected.

See [complete bounded repair YAML](examples/bounded-repair.yaml) and
[MicroSpec](docs/plans/flow-001.yaml). Example executable paths are local examples
and must be adjusted; the loader does not search PATH or launch them for validation.

Paths use the lexical absolute flow path provided by caller: loading B/flow.yaml
symlinked to A/flow.yaml with workspace '.' uses B. The flow-file symlink is not
resolved to choose its base. Workspace itself resolves symlinks/.. to an existing
absolute directory. Current cwd only identifies a relative input filename; it does
not affect the meaning of an already identified absolute flow path.

The private SafeLoader copies implicit resolver dictionaries/lists before replacing
boolean resolution with lowercase true/false only. Thus bare on remains a string.
Normal yaml.safe_load behavior is unchanged before/after loading (tested). Every
mapping's scalar-string keys are checked for duplicates before dict construction.
Anchors, aliases, merge keys, explicit tags, empty/non-mapping roots and multiple
YAML documents are rejected. No coercion or fallback parser.

Validation layers: safe syntax/keys -> strict local schema/references -> existing
constructors -> existing runtime.validate(flow, executors). The public validate API
already existed; no new API and no copied graph algorithm were needed. Unbounded
cycles and invalid budget handlers remain domain errors, chained into FlowConfigError.
Examples of public errors:

```text
version: missing required field
version: unsupported flow configuration version: 2
executors.verifier.timeout_seconds: expected positive finite number, got str
states.fix.on.succes: unknown outcome/event
states.verify.terminal: only terminal: true is permitted; executable states omit terminal
states.fix.on.failed: duplicate mapping key (line 5, column 7)
```

Messages use logical paths (source locations for syntax/keys), cap length at 512,
and do not dump YAML/argv/credentials. Original exceptions are preserved as causes.

Verification on 2026-09-14: macOS 26.6.2 arm64, Python 3.14.7, PyYAML 6.0.3,
installed in isolated /tmp/flow001-venv. Full suite: **114 tests, 113 PASS, 1 skip**,
6.263 seconds. The skip is existing Windows batch-path validation. Prior 105 tests
are unchanged. All 22 FLOW-001 acceptance criteria passed on this platform.
No real provider/model calls or CLI commands were added/run for verification.

The real Command verifier + fake Codex executable boundary proved both accepted
and failed-repair escalation paths from YAML. Exact captured diagnostic bytes
produced the existing JSON-framed Codex argv. Python/YAML objects match, as do all
normalized Event fields and terminal states. Each flow schedules one repair
invocation, preserving existing refusal/handoff semantics. Symlink A/B and global
resolver isolation tests passed; missing-dependency import/load boundary was checked
in a clean interpreter; unsafe/duplicate/non-string keys and canonical states tested.

Before/after SHA-256 match for every runtime file:

```text
d1cde025d575410ec85bd29303dbcc14940699b065e659d8dfa1cf93106fd1fd  runtime/__init__.py
5ddf25faded6cdaa492472a894db8ec25c505ac52b55278e793d0986b36af4f7  runtime/claude.py
0dc95fd79a6176dbb895d8834c75feca622cb3a16ad89aa797d5494709491cba  runtime/codex.py
a637e5d59ab112be7f23a50ebd9c73b30c01e28f6187be6d7a8da733451e3d54  runtime/command.py
7bb866fba309ab7e824bb905b88aa4eba90d9c930e196b93aae24355f7defeeb  runtime/recording.py
```

## FLOW-002 source-tree Switchyard CLI

From repo, using an interpreter with requirements installed:

```sh
python3 -m switchyard validate examples/bounded-repair.yaml
python3 -m switchyard run path/to/flow.yaml --task "Fix the failing implementation"
python3 -m switchyard run path/to/flow.yaml --task "Fix the failing implementation" --require-terminal accepted --require-terminal skipped
```

No installed console script or package publishing. OBS-001 below adds opt-in CLI recording. From another
directory the source package must be importable (for example explicit PYTHONPATH).
No chdir or executor PATH resolution occurs. FLOW-001 schema is unchanged.

Exit codes: 0 = valid configuration or normal terminal satisfying any assertion;
1 = normal terminal outside required set; 2 = CLI/config/required-terminal validation
error; 3 = ExecutionError. Every normal terminal is operational success without
an assertion. Names have no business semantics. Every required name must exist and
be terminal before WorkUnit/runtime execution, even among other valid names.

Normal run prints existing run ID and terminal, plus requirements only on mismatch.
Exactly one run_started event is located by kind; missing/duplicate events are
internal errors. Normal output is flushed before returning 0/1. Output I/O failures
propagate and are not mapped to workflow errors. Known errors go to stderr with no
traceback. ExecutionError prints exactly `switchyard: runtime execution failed`;
its message, task, diagnostics, causes and events are never displayed.

One private helper JSON-quotes ambiguous names/paths, escapes control characters
and bounds known-error display. Comparison uses original strings. For example a
terminal containing a newline and comma appears on ONE line as:

```text
Terminal: "line\nnext, value"
```

Verification on 2026-09-14: macOS 26.6.2 arm64, Python 3.14.7, PyYAML 6.0.3.
`/tmp/flow001-venv/bin/python -m unittest discover -s tests -v`:
**124 tests, 123 PASS, 1 skip**, 7.809 seconds. Skip: existing Windows batch-path
validation. All prior 114 tests are unchanged; all 20 FLOW-002 criteria passed.
No real model/provider calls. Tests use real command subprocesses and controlled
fake Codex process boundaries, not real provider executables.

Real process-level checks (`python -m switchyard`, temporary fixtures):

| Scenario | Exit | Output |
| --- | --- | --- |
| validate valid.yaml | 0 | stdout Valid: absolute addressed path |
| validate version-2 YAML | 2 | stderr switchyard: "version: unsupported flow configuration version: 2" |
| run ending escalated without assertion | 0 | stdout Run / Terminal: escalated |
| escalated requiring accepted | 1 | stdout Run / Terminal / Required terminal: accepted |
| required accpeted typo | 2 | stderr switchyard: required terminal does not exist: accpeted |
| missing transition after command success | 3 | stderr switchyard: runtime execution failed |

Marker tests prove validate and invalid required terminals never execute commands.
Relative path and symlink B->A process tests confirm workspace B without chdir.
Newline/tab/comma/whitespace/quote/backslash names remain one output line and compare
exactly. Write/flush failures, semantic event lookup and unexpected-error propagation
are covered by main(argv) tests.

YAML bounded repair through main(argv) with fake Codex proves accepted exit 0,
escalated without assertion exit 0, escalated requiring accepted exit 1, accepted
requiring accepted exit 0 and multiple-required membership. Each run schedules
exactly one outer repair invocation; provider-internal token/turn budgets are not
claimed. CLI directly delegates configuration and execution to existing APIs.

SHA-256 before/after match:

```text
d1cde025d575410ec85bd29303dbcc14940699b065e659d8dfa1cf93106fd1fd  runtime/__init__.py
5ddf25faded6cdaa492472a894db8ec25c505ac52b55278e793d0986b36af4f7  runtime/claude.py
0dc95fd79a6176dbb895d8834c75feca622cb3a16ad89aa797d5494709491cba  runtime/codex.py
a637e5d59ab112be7f23a50ebd9c73b30c01e28f6187be6d7a8da733451e3d54  runtime/command.py
7bb866fba309ab7e824bb905b88aa4eba90d9c930e196b93aae24355f7defeeb  runtime/recording.py
b61f5c6ab6e1cc07822eed6fb2710bbefe5ae4302773503fb973b46b96475cb9  flow_config.py
```

Changed only switchyard/__init__.py, switchyard/__main__.py, switchyard/cli.py,
new tests/test_cli.py and tests/test_cli_process.py, README and FLOW-002 MicroSpec.
FLOW-003 has not started.


## OBS-001 CLI event recording

Record canonical runtime events by explicitly selecting a JSONL file:

```sh
python3 -m switchyard run path/to/flow.yaml --task "Check the implementation" --events-file events.jsonl
```

`--events-file PATH` is run-only. Relative paths are relative to the caller's
working directory, not the flow file or executor workspace. Parent directories
must already exist; the CLI creates no automatic `.switchyard` directory.
Without the option, execution and output are unchanged and no recorder is used.

The CLI delegates to the existing `JsonlRecorder`: events append in runtime order,
including when the file already contains previous runs. Each event is flushed and
closed; there is no fsync, crash-durability guarantee, retry or partial-log repair.
A failed write can leave a partial final line. Each run retains its runtime run ID.

`validate` and help never record; `validate --events-file ...` is a usage error
(exit 2). Invalid flow configuration or required-terminal names are rejected before
recorder construction. A missing parent or unusable destination fails when the
runtime first records, before executor invocation. Recording failures use the
existing `ExecutionError` boundary: exit 3, empty stdout, and only
`switchyard: runtime execution failed` on stderr. Available history remains on
disk when a later runtime or recorder failure interrupts a run.

Normal output remains the run ID and terminal, plus required terminals on mismatch.
A normal terminal mismatch still records the full run and exits 1. Task text,
diagnostics and event payloads are not printed by the recording option. Canonical
Event fields, runtime transitions and executor contracts are unchanged. Readers,
statistics, timing and correlation are outside this slice.

Verification (2026-09-25, macOS arm64, Python 3.14.7, PyYAML 6.0.3):
16 focused CLI tests passed; full offline suite ran 130 tests, with 129 passing
and one existing Windows-only skip. No model calls. Runtime, recorder and flow
configuration code are unchanged.
