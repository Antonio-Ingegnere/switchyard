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
    codex_models=(available_codex_model_id,), # exact locally available IDs, no alias map
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
Missing executable, unsupported effort/model or inconsistent profile fails resolution.
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

Model names pass unchanged via `--model`. The audited Claude set is `haiku`, `sonnet`,
`claude-haiku-4-5`, `claude-haiku-4-5-20251001`, and `claude-sonnet-4-6`.
Aliases intentionally follow the provider's alias; use a pinned ID when version stability
is required. Other names fail explicitly pending capability verification.

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
`codex_models` explicitly declares locally available IDs; it never renames them.
The canonical example's `codex` name is a sample identifier, not a promised public
model alias. An actual project must configure a model ID available to its installation.
Fake-process tests declare `codex` as their fake model, without claiming live availability.

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
