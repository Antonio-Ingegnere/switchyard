# V2 readiness and consumer surface

CONFIG-001 adds the [one-file configuration front end](../configuration.md) over this engine.
Normal consumers use `load_simple_config` / `compile_simple_config`; verbose bundles remain advanced.

Switchyard V2 is internally complete for bounded single-work-item FEATURE and
BUGFIX delivery, cross-provider escalation, deterministic verification, durable
context, manual QA, safe acceptance and reopen. No known internal prerequisite
remains before consumer migration within this scope. Consumer integration still
supplies project configuration, human authorization and repository ownership.

## Supported entry points

Use the existing named modules; no package-root façade or new wrapper is needed.

| Module | Public surface and purpose |
| --- | --- |
| `switchyard.workflow_bundle` | `load_workflow_bundle`, immutable `WorkflowBundle`, profile/role/state/artifact definitions; strict V2 loading |
| `switchyard.workflow_compiler` | `compile_workflow`, `CompiledWorkflow`, `CompiledStatePlan`, `ExecutionBinding`; inspection and custom executor resolution |
| `switchyard.workflow_execution` | `execute_v2_workflow`, `WorkflowExecution`; ordinary loader-to-runtime composition |
| `switchyard.provider_stages` | `ProviderRuntimeConfig`, `ProviderStageExecutorResolver`; machine-local executables/environment/model aliases |
| `switchyard.context_transport` | `workspace_resolver`, `StageExecutor`, `StageInvocation`, `StageResult`, `ProducedArtifact`, `StageHandoff`, `ArtifactSnapshot`, output-contract records; custom stage integration |
| `switchyard.work_contract` | `WorkWorkspace`, artifact/producer/handoff records, `validate_microspec`; versioned verified durable storage |
| `switchyard.qa_lifecycle` | `create_qa_candidate`, `accept_v2_work`, `reopen_v2_work`, `QaCandidate`, `QaDecision`, lifecycle exceptions |
| `runtime`, `runtime.recording`, `switchyard.observability` | `run`, `WorkUnit`, `RunResult`, `JsonlRecorder`, `read_events`, projections; execution and recorded-history reconstruction |

Start with the [canonical FEATURE/BUGFIX bundle](../../examples/delivery/workflows-v2.yaml).
Application composition is:

```python
from runtime.recording import JsonlRecorder
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.workflow_execution import execute_v2_workflow
from switchyard.provider_stages import ProviderRuntimeConfig
from switchyard.qa_lifecycle import create_qa_candidate, accept_v2_work, reopen_v2_work

bundle = load_workflow_bundle("examples/delivery/workflows-v2.yaml")
# Supply target, executables, environment and optional model_aliases locally.
config = ProviderRuntimeConfig(working_directory=target,
                               claude_executable=local_claude, codex_executable=local_codex,
                               environment=local_environment, model_aliases=local_aliases)
execution = execute_v2_workflow(bundle, "feature", request, delivery_root, config,
                                recorder=JsonlRecorder(events_path))
if execution.result.terminal_state == "READY_FOR_MANUAL_QA":
    candidate = create_qa_candidate(target, execution.workspace, execution.result,
                                    "feature", fresh_external_qa_root)
# Later, only after the caller obtains the human's decision:
# accept_v2_work(target, candidate.workspace)
# or reopen_v2_work(target, candidate.workspace, human_feedback)
```

Use fresh delivery roots per work item/run; the generic runner is not a crash-resume
engine. Preserve the bundle/config reference and events outside the target Git tree.
The QA workspace is also fresh and external. Candidate creation snapshots the entire
eligible worktree, so the caller must isolate unrelated work **before** review.
Ignored untracked files are excluded; pre-existing staging blocks. See the
[exact QA safety policy](work-contract.md#human-qa-lifecycle-v2-007).

Completed runs can be reconstructed from `read_events(events_path)`, validated with
`summarize_runs`, and `RunResult(last_run_finished.state, events)`. Reopen the source
with `WorkWorkspace.open(delivery_root)`. No serialized Python object or session ID
is needed. For multi-run logs select and validate the intended complete run first.
The [executable offline lifecycle example](../../examples/delivery/offline_lifecycle.py)
proves this across a subprocess, including a reopened candidate workspace. Its
`qa_feedback` text is directly readable as input for a later caller-controlled run.

Internal mechanisms include `ContextualExecutor`, concrete provider stage classes,
provider-process plumbing, compiler lowering helpers, Git guards/private index
implementation, and workspace publication helpers. They are not consumer hooks.
V1 loaders/CLI/runtime adapters and DELIVERY-001 presets remain supported legacy
compatibility surfaces. V2 does not replace their contracts.

## Canonical configuration and examples

Canonical model/effort/capability settings remain frozen:

- FEATURE SPECIFY and BUGFIX TRIAGE/SPECIFY_FIX: Sonnet medium, repo_read.
- REPRODUCE and primary DIAGNOSE: Haiku medium, repo_read + command.
- IMPLEMENT/FIX/REPAIR_ONCE/REFIX: Sonnet medium, read + write + command.
- Diagnostic ladder: Haiku → Sonnet high/continue → Codex high/fresh;
  REDIAGNOSE uses Codex high. State and rung limits compose with run limits.
- VERIFY/VERIFY_AGAIN use deterministic command profiles and exit mapping.

All four canonical profiles are referenced; no stale model profile or local alias
is embedded. `triage` and `revised_fix_microspec` are currently unused catalog names,
not dead execution profiles. They remain to avoid changing durable catalogs or
historical contracts for cosmetic cleanup.

| Example | Classification | Starting point |
| --- | --- | --- |
| delivery/workflows-v2.yaml | canonical delivery | FEATURE or BUGFIX |
| escalation/workflows-v2.yaml + factual markdown | cross-provider/escalation proof | three singleton outcomes; continue then fresh; no tools |
| delivery/offline_lifecycle.py | lifecycle/acceptance | fake delivery, actual verifier/Git, restart, accept/reopen |
| delivery/dogfood_feature.py and dogfood_bugfix.py | canonical delivery / escalation proof harnesses | retained live evidence; **not** automatic smoke commands |
| docs' inline loader/compiler/workspace snippets | minimal instructional examples | small programmatic composition |

The top-level legacy flow examples and DELIVERY-001 CLI are not V2 bundles.
No obsolete duplicate V2 example or safely removable production dead code was found.

## Failure and restart boundaries

| Boundary | Publication order / possible interruption | Reopen and retry policy |
| --- | --- | --- |
| Artifact | validate → fsynced `.pending` → no-overwrite link → manifest append/fsync | Manifest is authority; bytes verified on read. Orphan target/pending/lock or partial manifest requires inspection; no automatic repair or blind retry. |
| Handoff | validate → numbered file → manifest append | Same rules; committed artifacts may precede an interrupted handoff write. |
| Invocation | compiled outcome + all artifacts/handoff validated → artifacts in order → handoff → runtime response/events | Invalid outputs commit nothing. I/O failure may leave a prefix. Runtime/recording failure is not permission to replay providers. |
| Event recorder | event observed in memory → JSONL append/flush | Partial tail may remain; recorder has no fsync guarantee. Require complete valid recorded terminal for restart recipe; no inferred completion. |
| QA creation | double Git snapshot → new catalog → immutable request → candidate | Partial workspace is not a candidate. Missing candidate blocks; inspect leftovers, use a fresh root after explicit review. |
| Accept reservation | lifecycle lock → intent v1 | Unresolved intent/stale lock blocks replay. |
| Private staging | real index lock → private base index → explicit candidate entries → tree checks | Before publication original index/worktree unchanged. Temporary files/locks may need manual cleanup after process death. Never reset worktree. |
| Commit publication | immutable commit object → prepared intent with hash → old-HEAD CAS → prepared index installation → invariant checks → decision | Crash can leave orphan commit, advanced HEAD, old index or missing decision. Inspect prepared hash/refs/index; never retry or auto-reset. |
| Reopen | intent → feedback → immutable decision | Feedback may exist without decision; unresolved intent blocks replay. No Git mutation. |
| Finalized decision | immutable decision checked before any later operation | Repeated accept/reopen explicitly rejected; a new review requires a new candidate. |

This is local append-only storage with cooperative exclusive writers, not a
multi-file or distributed transaction. File fsync does not claim directory-fsync
power-loss guarantees. Existing crash/partial-state regressions and fail-closed
manual recovery remain the contract; no transaction engine was introduced.

## Error handling

- `FlowConfigError`: invalid local YAML/schema; `WorkflowCompileError`: graph or
  executor resolution failure. Configuration diagnostics may name caller-provided
  configuration, which must not contain credentials.
- `ProviderResolutionError`: unavailable executable/model or unsupported capability
  tuple, before invocation. Keep machine-specific aliases external.
- `ProviderExecutionError`: process/transport boundary; bounded sanitized failure
  diagnostics, no workflow retry. Redaction is best effort, not a secret store.
- `WorkContractError`: exact contract location/category. Rejected semantic outcome
  and undeclared artifact names are not echoed. Invalid artifacts/handoffs never
  publish any output from that invocation.
- `ExecutionError`: runtime failure with observed events/cause; budget exhaustion
  follows configured control-plane routes rather than masquerading as provider failure.
- Lifecycle returns `ACCEPTED`, `REOPENED`, or `COMMIT_BLOCKED` with fixed reason codes.
  `LifecycleError` rejects finalized/invalid operations; `LifecycleRecoveryRequired`
  signals unresolved publication/audit and requires inspection. A post-commit error
  explicitly retains the committed hash and never promises rollback.

No new exception hierarchy or automatic retry was added. The V2-008 consistency
correction prevalidates the already-compiled outcome set before persistence; runtime
still owns routing and independently validates outcomes.

## Evidence and limitations

Live evidence is historical and preserved: [V2-005 FEATURE](../plans/v2-005-feature-dogfood.yaml),
[V2-006 BUGFIX attempt 7 and Claude → Claude → Codex escalation](../plans/v2-006-bugfix-dogfood.yaml).
Both deliveries reached READY_FOR_MANUAL_QA; escalation reached ASSESSED.
No live calls were repeated for readiness.

Offline coverage proves budgets, fresh/continue context, strict outputs, real
verification, exact-scope commits, drift blocking, durable reopen feedback and
completed-run reconstruction across processes. [V2-008's record](../plans/v2-008-final-readiness.yaml)
contains final counts and disposable dogfood evidence.

Known limits:

- Successful provider command-tool use is not independently observed through current
  adapters. Durable reproduction evidence can be compared with an independent baseline.
- Claude supports explicit no-tools, read, read/write, read/command and
  read/write/command. Codex supports no-tools, read/command and read/write/command;
  unsupported tuples fail before invocation. Tool policy is not a security sandbox
  against a malicious executor. See [provider contract](provider-stages.md).
- No automatic provider retry, native session authority, runtime crash-resume or push.
- Cooperative exclusive worktree access is required during QA; no hostile-writer guarantee.
- The local synthetic SIGKILL test case remains a known environment failure; the
  Windows-only platform skip remains. Neither is disabled for freeze verification.

Project/epic planning, DAG scheduling, dashboards, tool telemetry, network/browser
capabilities, PR/push automation, distribution, server/GUI and release machinery
are intentionally outside V2. Consumer migration is separate work; Adeo is untouched.
