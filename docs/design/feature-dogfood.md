# Real FEATURE dogfood

`switchyard.workflow_execution.execute_v2_workflow(bundle, flow_name, request,
work_root, provider_runtime_config, recorder=None)` composes the existing workspace,
provider resolver, context resolver, compiler and runtime. Its frozen result contains
`result` (RunResult), `workspace` and `compiled`. Exceptions propagate; it does not retry.
The API is not FEATURE-specific.

The manual entrypoint makes **real, potentially billable provider calls**:

```sh
.venv/bin/python examples/delivery/dogfood_feature.py \
  --claude claude --codex codex --model-aliases /path/to/local-aliases.json
```

The optional alias file is the provider/selector mapping accepted by
ProviderRuntimeConfig. Keep machine/account-specific values outside this repository.
Executable names resolve using the current PATH. Default per-process timeout is 600
seconds; `--timeout` changes it explicitly. No model or effort substitution occurs.
Check `claude auth status` before running; the first real invocation remains the
account/model capability check. Compilation resolves every configured rung eagerly,
including Codex; this does not require a live Codex call on the straight path.

The harness copies `tests/fixtures/feature_dogfood_project` into a new temporary
`target/`, uses a separate `work/`, and always retains both. It never uses the active
Switchyard repository as the agent working directory. No target commits or pushes.
The natural request asks for optional case-insensitive exact catalog lookup while
preserving default behavior. Before any provider call, the canonical unittest command
(with verbose reporting) must show two passing existing-behavior tests and exactly
one expected acceptance failure. A green or unrelated failing baseline aborts.

The canonical feature flow runs unchanged, once. Only offline tests supply a fake
model process that implements the fixture. Live work is done solely by provider
processes. Command verification is real in both paths. A failing VERIFY may follow
the normal diagnosis/repair graph; the harness adds no retries.

Evidence includes `events.jsonl`, `observations.jsonl` (source hashes and durable
contract observations at invocation boundaries), `baseline-files.json`,
`baseline-verification.txt`, `changed-files.json`, `report.json`, and the normal work
manifest/artifacts/handoffs. Original acceptance tests must remain unchanged; the
agent may add tests. Provider operational files and bytecode do not count as source
changes. Success requires a real IMPLEMENT change and verified READY_FOR_MANUAL_QA.

A provider/framework failure exits nonzero and retains evidence without repairing
artifacts, source or output. No full prompts, credentials or provider transcripts
are added to the execution report. Bounded transport-error diagnostics may be included.
The retained directories may be removed manually after inspection.
