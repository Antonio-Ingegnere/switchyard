# V2 provider stage execution

`switchyard.provider_stages` supplies `ClaudeStageExecutor`, `CodexStageExecutor`,
`CommandStageExecutor`, and `ProviderStageExecutorResolver(profiles, runtime_config)`.
The resolver accepts `bundle.profiles` (also preserved in compiled inspection) and
is a callable from ExecutionBinding to StageExecutor. It resolves each executable
at compilation, without launching a process or reading YAML again.

```python
config = ProviderRuntimeConfig(
    working_directory=project_root,          # absolute existing directory
    claude_executable=claude_path,           # explicit path or executable name
    codex_executable=codex_path,
    model_aliases={"codex": {"codex": locally_configured_model_id}},
    timeout_seconds=600,
)
provider = ProviderStageExecutorResolver(bundle.profiles, config)
resolver = workspace_resolver(bundle, flow_name, workspace, provider)
compiled = compile_workflow(bundle, flow_name, resolver)
result = run(WorkUnit(request), compiled.flow, compiled.executors)
```

Configuration is frozen. `environment=None` snapshots the current environment;
an explicit mapping replaces it and is copied read-only. It is excluded from repr.
`max_result_bytes` defaults to 1 MiB. Command argv[0] resolves through this environment's
PATH, or relative to working_directory when it contains a path component. The rest
of argv is unchanged. No shell, interpolation, provider substitution or command registry.
Missing executable, malformed selector mapping, unsupported effort translation or inconsistent profile fails resolution.
Credential/account access cannot be established offline; provider rejection remains
an explicit execution failure.

## Audited protocols

Local inspection used Claude Code **2.1.282** and Codex CLI **0.157.0**, help/version only.

Claude uses `--print --output-format json --json-schema <schema>` with stdin text and
subprocess cwd. Only `structured_output` from a successful result envelope is parsed.
Operational stderr and the envelope's human-readable result are not semantic output.
This is the documented [headless structured output protocol](https://code.claude.com/docs/en/headless).
The adapter retains restricted file tools, acceptEdits and disabled permission prompts;
safe-mode and strict empty MCP configuration prevent loading user/project customizations.
It never supplies resume/continue/session flags, and disables configured model fallback.

Workflow `model` is a **provider model selector**. Resolution first looks up the exact
provider and selector in `model_aliases`; absent a mapping, the selector passes through
unchanged. Mappings are single-step, not recursive. Both mapping levels are copied
read-only; names/values must be nonempty strings without NUL (no coercion or trimming).
The old `codex_models` allowlist is removed. No model cache is read or required.

`ExecutionBinding.model` remains the workflow selector. `ClaudeStageExecutor.resolved_model`
and `CodexStageExecutor.resolved_model` expose the concrete argument sent explicitly via
`--model`. The rendered binding remains unchanged. Neither canonical Event nor compiled
plan semantics change. Compilation still resolves every rung once, including unused
branches; it validates local configuration and tooling, not authenticated entitlement.
A provider rejecting a model is an execution failure, with no alternative model retry.

Claude's direct `haiku`/`sonnet` selectors need no local mapping. Its audited effort
translation set is `haiku`, `sonnet`, `claude-haiku-4-5`, `claude-haiku-4-5-20251001`,
and `claude-sonnet-4-6`. Effort policy follows the resolved concrete model. Unknown
Claude models pass through when no effort is requested; requested effort fails locally
if its translation is not audited. This is not a model availability check.

Sonnet generic low/medium/high maps identically to native `--effort`.
Haiku lacks native effort. Its explicit adapter translation is an extended-thinking
budget: low=1024, medium=8192, high=16384 via `MAX_THINKING_TOKENS`, with output tokens
set to 32768 and thinking enabled. This is a configurable-depth policy, not equivalence
between Haiku and Sonnet reasoning or a native Haiku effort flag. See the official
[model effort capabilities](https://code.claude.com/docs/en/model-config) and
[thinking-budget environment contract](https://code.claude.com/docs/en/env-vars).
Inherited conflicting effort/thinking and default-model overrides are removed for
this invocation. Managed/server policy may still constrain a deployment; these tests
prove requested transport configuration, not remote policy or model quality.
`MAX_STRUCTURED_OUTPUT_RETRIES=1` disables the CLI's additional schema-repair attempts.

Codex uses exact `--model`, `-c model_reasoning_effort="low|medium|high"`, stdin `-`,
`--output-schema <temporary schema>`, and `--output-last-message <temporary result>`.
It retains ephemeral execution, ignored user config/rules, workspace-write sandbox and
approval never. cwd and --cd agree. Only the final-result file is parsed; stdout/stderr
are operational logs. See [noninteractive output](https://developers.openai.com/codex/noninteractive)
and [reasoning configuration](https://developers.openai.com/codex/config-reference).
The canonical `codex` selector can be mapped to a locally configured concrete ID without
editing workflow YAML. Exact model IDs also pass through with no redundant alias entry.
Unknown selectors are not rejected based on cache membership. Only live provider
execution can establish account/model availability. A local configuration or cache
may inform an explicit mapping, but is never authoritative evidence of entitlement.
Executable paths and machine-specific mappings stay outside committed workflows.

Both adapters launch exactly one process per stage. Provider-native tool turns and
bounded transport retries inside their CLIs are distinct from Switchyard semantic
attempts. The adapters add no retry/escalation loop. No real-provider smoke run was used.

## Structured input and output

`render_invocation(StageInvocation)` produces deterministic sections for binding,
selected artifact name/version/format/schema/content, optional advisory prior handoff,
existing diagnostic input and an output schema. It reads no workspace, manifest or
provider history. Repository access remains the CLI's configured tool access.
Fresh/continue selection remains entirely in ContextualExecutor.

`result_schema()` returns a fresh JSON Schema. The StageResult envelope requires
exactly `outcome`, `produced_artifacts`, `handoff`, `diagnostics`. Artifact objects
have name/content. Handoff is null or exactly summary/findings/evidence/
changed_assumptions/blockers. Diagnostics is null or a string converted to the existing
4096-byte DiagnosticPayload. Parsing rejects duplicate keys/names, nonfinite numbers,
wrong types, extra fields, invalid UTF-8, malformed handoffs and reserved budget_exceeded.
No prose extraction or repair is attempted. ContextualExecutor still checks artifact
contracts and commits outputs; runtime alone validates declared semantic outcomes.

## Shared process boundary and commands

The V2 helper reuses existing Command validation, nonblocking read, child cleanup and
diagnostic normalization utilities. Runtime and old executors are unchanged. It uses
one shell-free Popen, file-backed stdin, bounded incremental pipe retention and a
monotonic timeout. Interrupts clean up and propagate. Spawn, I/O, timeout, signal and
provider nonzero exit raise ProviderExecutionError. Its optional diagnostics attribute
uses DiagnosticPayload. Configuration failures use ProviderResolutionError.

Command normal exits alone map through configured exit_codes/default. Optional capture
returns a ProducedArtifact on every normal exit, and handoff remains None. Combined
capture redirects stderr into the stdout OS pipe; order is observed pipe byte order,
not a reconstructed cross-stream timeline. stdout/stderr capture selects only that
stream. Incremental UTF-8 replacement decoding precedes tail retention. The evidence
starts with '=' for complete output or '!' for truncated output; the marker is included
in max_bytes. Even a one-byte cap reports truncation. No command output is interpreted
as LLM JSON, and no transport failure is turned into the default semantic outcome.

Structured stdout aborts on overflow; Codex's separate result file is monitored and
read with a limit. Diagnostic retention is 4096 bytes. Capture limits bound retained
memory/output, not a hard filesystem quota on a provider-owned result file. Cleanup
kills/waits the direct child, as the existing helper does; process-tree cancellation
is not guaranteed. Descendant-held pipes cannot extend the configured deadline.
Tests use POSIX fake executable scripts; Windows fixture tests are skipped.

## Stage-specific output guidance (V2-003C)

The shared renderer now includes `StageInvocation.output_contract` as deterministic
STAGE OUTPUT CONTRACT JSON plus instructions: return exact logical names (not paths),
only allowed semantic outcomes, raw declared content format, and all required MicroSpec
fields from the shared validator descriptions. Artifact production remains optional.
Handoff guidance explains body-only ownership and logical evidence references.

`result_schema(contract=None)` retains the generic StageResult envelope. Both real
adapters pass the invocation contract to specialize outcome and artifact-name enums;
no allowed artifacts means maxItems=0, not an invalid empty enum. Claude's --json-schema
and Codex's --output-schema receive equivalent schemas. The generic strict parser,
context post-validation and runtime outcome validation remain authoritative. No retries,
provider sessions, command semantics or context-selection changes are introduced.

## Exact capability support matrix (V2-004B)

`ExecutionBinding.capabilities: tuple[str, ...] | None` transports the logical
state policy to every resolved rung. `None` retains legacy behavior: Claude's
original restricted file tools; Codex's original workspace-write execution.
Explicit policies are checked before invocation, including eager compilation.
Errors identify flow, state, rung, provider and requested capability combination.

| Explicit capabilities | Claude tools | Codex configuration |
| --- | --- | --- |
| `[]` | empty `--tools` | `--sandbox read-only`, `features.shell_tool=false` |
| `repo_read` | Read, Glob, Grep | rejected |
| `repo_read, repo_write` | Read, Glob, Grep, Edit, Write | rejected |
| `repo_read, command` | Read, Glob, Grep, Bash | read-only sandbox, shell enabled |
| all three | Read, Glob, Grep, Edit, Write, Bash | workspace-write sandbox, shell enabled |

Write or command without read is rejected by the loader; provider resolution also
rejects such manually constructed bindings. There is no implicit capability expansion.
Claude's nonempty explicit tool list is supplied to both `--tools` (availability)
and `--allowedTools` (permission), retaining restricted mode, disabled permission
prompts and strict empty MCP configuration. No browser or MCP tools are added.
Codex retains `--ask-for-approval never` and uses the supported shell feature toggle;
read-only describes its filesystem policy for commands, not absence of commands.
No-tools Codex may reason from supplied artifacts and return the same structured
StageResult; no alternative repository backend or MCP server is installed.

The shared renderer describes explicit grants, or states that an empty policy must
work only from supplied durable context. Legacy omission adds no capability section.
This is tool configuration, not a universal security sandbox: in particular Claude
Bash can run commands with side effects even when Edit/Write are absent. Switchyard
does not promise network isolation or confinement of every child process. Provider
sandbox/permission enforcement remains provider-owned. Existing stage timeout,
configured cwd, workflow invocation budgets and one-process adapter behavior remain
in force; no Switchyard-side shell tool or retry loop is added.

Audit sources: installed Claude 2.1.282 help (`--tools`, `--allowedTools`,
`--restricted`, `--permission-prompts`) and Codex 0.157.0 help/features, plus the
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
The unsupported independent Codex read-only tool mapping is handled by rejection,
not an approximation. No live provider invocation was used to validate this slice.
