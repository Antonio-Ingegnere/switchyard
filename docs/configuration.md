# One project configuration

Save this complete configuration as `.switchyard/config.yaml`. It runs a bounded
specification → implementation → deterministic verification flow:

```yaml
agents:
  spec:
    provider: codex
    model: gpt-6-astra
    effort: high
    workspace: read
    shell: true
    timeout: 600
  code:
    provider: claude
    model: sonnet
    effort: medium
    workspace: write
    shell: true
    timeout: 1800
  tests:
    provider: command
    command: [python3, -m, unittest, discover, -s, tests]
    capture: verifier_evidence
flows:
  feature:
    start: SPECIFY
    limits: {run_invocations: 3}
    states:
      SPECIFY:
        agent: spec
        consumes: [request]
        produces:
          microspec: {format: yaml, schema: microspec}
        on: {ready: IMPLEMENT, needs_clarification: NEEDS_HUMAN}
      IMPLEMENT:
        agent: code
        consumes: [request, microspec]
        produces:
          implementation_handoff: {format: markdown}
        on: {ready: VERIFY, blocked: NEEDS_HUMAN}
      VERIFY:
        agent: tests
        consumes: [microspec, implementation_handoff]
        produces:
          verifier_evidence: {format: markdown}
        on: {pass: READY_FOR_MANUAL_QA, fail: NEEDS_HUMAN}
lifecycle:
  manual_qa: true
  accept: {commit: true}
  reopen: true
```

For complete FEATURE **and** BUGFIX with bounded repair and escalation, copy
[the full default](../examples/simple/config.yaml). Adeo commits that configuration
at `.switchyard/config.yaml`, with its command agent running `python3 scripts/verify-delivery.py`.
There are no role, profile, provider-override or model-alias files to edit.

## Everyday changes

- Change the specification model: edit `agents.spec.provider` / `model` / `effort`.
  Every state referencing `agent: spec` changes together.
- Change who implements: change that state's `agent` to another defined agent.
- Change the flow: edit `flows.<name>.states` and their `on` transitions.
- Change verification: edit the command agent's `command` argument list.
- Inspect without an LLM or installed provider executables:

```sh
python -m switchyard config validate
python -m switchyard config show
# An explicit file is optional:
python -m switchyard config show examples/simple/config.yaml
```

Run these with Switchyard on Python's import path and its requirements installed.
`show` prints every effective invocation, provider, direct model, effort, workspace,
shell, outcome restriction, inputs/outputs, command, graph/budgets and lifecycle policy.
It never consults an alias file. Authentication uses CLI login or environment secrets;
never put credentials in YAML. Executable locations/PATH are launch environment, not
project model/flow configuration.

## Keys and defaults

Only `agents`, `flows` and optional `lifecycle` exist at the top level.
Unknown fields, duplicate keys, YAML anchors, aliases and merge keys fail validation.
Logical names start with a letter and contain letters, digits, `_`, `.` or `-`.

Model agents require `provider: claude|codex`, `model`, `workspace: none|read|write`,
and boolean `shell`. Optional `effort: low|medium|high` defaults to the adapter's
unspecified effort. A direct model is passed to the existing adapter unchanged.
`workspace: write` includes read. `shell: true` allows command execution under the
provider's sandbox; it does not promise independent tool-use telemetry.

Provider restrictions fail during configuration validation. Currently Codex supports
`none/false`, `read/true`, `write/true`; Claude also supports `read/false` and
`write/false`. Neither supports `none/true`. Claude effort translation remains audited
for its existing Haiku/Sonnet model names; this front end does not expand adapter support.

Command agents require `provider: command` and `command: [executable, args...]`.
They execute the explicit argv in the target directory with no shell interpolation.
Optional `success` maps exit zero (default `pass`); `failure` maps all other exits
(default `fail`). Optional `capture` names a declared text/markdown output; combined
stdout/stderr is bounded to 32768 bytes. Command agents do not take model permissions
or effort: the command itself is deterministic code authorized by this file.
A command state's `consumes` reach the process as read-only files named after each
artifact in the private directory `$SWITCHYARD_ARTIFACTS` (outside the worktree,
removed after the process). A verifier can thereby compare against durable evidence,
such as a baseline produced by an earlier command state.

Each flow requires `start` and `states`. Optional `limits` has `run_invocations`
(default sum of state invocation budgets) and `on_exceeded` (default `NEEDS_HUMAN`).
Each executable state requires `agent` **or** `run`, and `on: {outcome: target}`.
Optional `consumes` is a list (default empty). `request` is the immutable human input.
Other inputs require a reachable producer before the consumer in the same flow;
missing inputs on a particular execution path still fail at the durable context boundary.
Optional `produces` maps names to `{format: text|markdown|yaml|json, schema: microspec}`;
`schema` is optional, supported only with YAML/JSON. Repeated producers must declare
identical format/schema. No global artifact catalog is needed.

Optional state `budget` accepts `invocations` (default sum of run-rung invocation
limits), `visits` (default 1), `on_exceeded` (default `NEEDS_HUMAN`). Limits are positive
integers. Every invocation, state visit and run retains the exact V2 accounting rules.
`READY_FOR_MANUAL_QA`, `NEEDS_HUMAN`, `NEEDS_REDIAGNOSIS` are implicit terminals when
referenced; other terminals use `name: {terminal: true}`. Unknown targets fail.
Only `NEEDS_HUMAN` and `NEEDS_REDIAGNOSIS` may additionally declare
`resume_to: <nonterminal state>`, the single state an explicit human resume may enter
(see [recovery](design/delivery-recovery.md#human-recovery-from-needs_-terminals)).
`ACCEPTED`, `REOPENED`, `COMMIT_BLOCKED` are lifecycle results, not agent transitions.

## Escalation: one explicit ladder syntax

```yaml
run:
  - agent: investigate
    outcomes: [ready, inconclusive]
  - agent: diagnose-deep
    when: [inconclusive]
    context: continue
    outcomes: [ready, diagnosis_disproved]
  - agent: independent
    when: [diagnosis_disproved]
    context: fresh
    outcomes: [ready]
on: {ready: NEXT}
on_exhausted: NEEDS_HUMAN
```

The first rung has `agent`; later rungs require `agent`, `when`, `context`. Every rung
may restrict `outcomes` and set `budget: {invocations: 1}` (default 1).
Same provider means vertical/continue; a different provider means horizontal/fresh.
All agents in a state must have identical permissions, matching V2's shared state policy.
Triggers are V2's existing `inconclusive`, `budget_exceeded`, `diagnosis_disproved`,
`verification_same_failure`. `budget_exceeded` is runtime-only, never a model outcome.
Omitted `outcomes` uses the existing V2 routable outcome set. Omitted `on_exhausted`
uses `NEEDS_HUMAN`. No rung skipping, retries, coercion or native session continuation.
A ladder is strictly sequential: rung N escalates only on rung N+1's `when`. A trigger
listed only on a later rung is unreachable from earlier rungs; there it exhausts the
ladder (`on_exhausted`) unless `on` routes it. List it on every intermediate rung.
Fresh excludes the previous handoff while retaining durable factual inputs.

## Human QA and consumers

`lifecycle.manual_qa` defaults true. `accept.commit` and `reopen` default to that value.
Both require manual QA. `commit: false` disables acceptance; it does not mark uncommitted
work ACCEPTED. `manual_qa: false` ends delivery without creating a QA candidate.
Consumers enforce this policy before calling unchanged V2 lifecycle APIs. Humans
choose accept/reopen; deterministic Switchyard code owns exact-scope commit safety.

```python
from switchyard.simple_config import load_simple_config, compile_simple_config
from switchyard.workflow_execution import execute_v2_workflow

project = load_simple_config(".switchyard/config.yaml")
bundle = compile_simple_config(project)
execution = execute_v2_workflow(bundle, "feature", human_request, work_root, launch_config)
# After READY_FOR_MANUAL_QA, if project.lifecycle.manual_qa:
# create_qa_candidate(..., external_persistent_qa_root)
# Later, on human decision and enabled policy: accept_v2_work / reopen_v2_work.
```

Profiles/roles/catalogs are generated internally and validated by the same V2 loader
and compiler. Runtime/Event, capabilities, StageResult, artifacts, handoffs, verification
and git/lifecycle safety are unchanged. Verbose Workflow Bundle V2 remains an advanced
compatibility format; advanced `ProviderRuntimeConfig.model_aliases` remains optional.
Adeo's normal path uses neither. Runtime workspaces and immutable per-run config snapshots
are evidence, not additional editable configuration. No push or automatic provider retry.

## Invocation deadlines and explicit recovery

Set `agents.<name>.timeout` to a finite positive number of seconds (default 600).
Booleans, strings, zero, negative numbers, NaN and infinity fail validation at that
agent field. Command agents accept the same key. `config show` includes the effective
timeout. No separate runtime or consumer override is needed.

For resumable delivery, use the [recovery API](design/delivery-recovery.md). A provider
failure or timeout before any StageResult is an infrastructure failure: it uses none of
the semantic `budget.invocations`, only the agent's `infrastructure_failures` allowance
(positive integer, default 2; resume refuses once the agent reaches it). It is frozen
with the rest of the semantic configuration.
Resume permits timeout-only edits and retains every prior budget charge. It never
automatically retries or repeats completed stages.
