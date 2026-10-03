# Workflow bundle v2

A bundle is a project-facing description, not an executable flow. The only loader is
`switchyard.workflow_bundle.load_workflow_bundle(path) -> WorkflowBundle`.
It reads UTF-8 YAML, validates all definitions and references, and returns immutable
values. It does not compile, invoke providers, resolve executables or process artifacts.

```python
from switchyard.workflow_bundle import load_workflow_bundle

bundle = load_workflow_bundle("examples/delivery/workflows-v2.yaml")
feature = bundle.flow("feature")
bugfix = bundle.flow("bugfix")
print(feature.entry, bugfix.entry)  # SPECIFY TRIAGE
```

`bundle.flow(name)` raises `FlowConfigError` for an invalid/unknown name. All loading
failures, including unreadable files and malformed UTF-8/YAML, use that existing
`flow_config.FlowConfigError` boundary and useful configuration paths. No version
autodetection or dispatch occurs: `load_flow()` accepts only FLOW-001 version 1;
`load_workflow_bundle()` accepts only version 2. Existing runtime/CLI behavior is unchanged.
The v2 module reuses the existing loader's internal strict YAML parsing helpers;
it does not call its executor construction or runtime validation.

## Exact schema

Required fields below are marked **required**. Every other field is optional.
Unknown fields are errors at every level; no metadata escape hatch exists.

| Location | Fields |
|---|---|
| Root | **version: 2**, **profiles**, **roles**, **flows**, `artifacts` |
| `artifacts.<name>` | **format**, `schema`, `source` |
| `profiles.<name>` | **provider**, **model** for agent providers, **command** for command provider |
| `roles.<name>` | **primary**, `escalation` |
| `primary` | **profile**, `effort`, `budget` |
| Each `escalation` item | **kind**, **on**, **profile**, **context**, `effort`, `budget` |
| `flows.<name>` | **entry**, **run_budget**, **states** |
| Normal state | **role**, **on**, `consumes`, `produces`, `budget`, **escalation_exhausted when the role has a ladder** |
| Terminal state | **terminal: true**, and nothing else |

`profiles`, `roles`, `flows` and each flow's `states` are nonempty mappings. Normal
state `on` is a nonempty mapping of project-defined outcome labels to state names.
References, mapping names, models, outcomes and artifact names match
`[A-Za-z][A-Za-z0-9_.-]*` exactly. They are logical identifiers, not paths or prose.
No trimming, string/number coercion, credential or secret fields are supported.
Command argv is static project configuration; executable availability is resolved locally later. Model identifiers are not checked against live provider catalogs.

Providers are closed to `claude`, `codex`, and `command` for this first schema.
Agent profiles require a model and reject command. Command profiles require command
and must omit model, including null. See the command capability contract below.
`human` is not a provider. Human boundaries are ordinary project terminal states.
Profiles cannot contain effort, roles, workflow semantics or budgets.

Invocation effort, when supplied, is exactly `low`, `medium`, or `high`. Omission
is represented by `None` and leaves the choice unspecified for future compilation;
there is no default translation here. Command bindings must omit effort, even null.
Roles refer to profiles; states refer only to roles. Unused profiles/roles are allowed
but still fully validated. Declaration order is retained in immutable mappings.

`consumes` and `produces` are lists, defaulting to empty tuples. Every item is a
logical identifier; duplicates within either list are rejected. The same artifact
may appear in both lists or across multiple states, except immutable request cannot
be produced. Every reference must exist in the artifact catalog. External inputs need
not have a producer in the bundle. Loader validation performs no filesystem probes;
storage/content validation is supplied separately by V2-003A, while consumption-time
availability, lineage and freshness selection remain deferred.

## Escalation

An omitted or empty escalation list means no escalation. Otherwise list order is
significant. Every rung is checked against its immediate predecessor, starting with
`primary`. The YAML step embeds profile/effort/budget; the immutable result exposes
those three fields together as `EscalationStep.binding: InvocationBinding`.

Accepted trigger names in a nonempty, duplicate-free `on` list:

- `inconclusive`
- `budget_exceeded`
- `diagnosis_disproved`
- `verification_same_failure`

These are declared control-plane values, not executor outcomes or implemented routing.
Normal state transition labels are project-defined identifiers and are not restricted
to this list or translated to the current runtime's outcome vocabulary.

Vertical escalation requires the same provider, `context: continue`, and a different
profile reference and/or effort. Changing only the budget is a no-op and is rejected.
There is no model ranking; even effort changes are checked structurally, not ranked.
Two distinct profile names satisfy a profile change regardless of model equivalence.
Horizontal escalation requires a different provider and `context: fresh`. Context
construction and reasoning-lineage enforcement are deferred. These are structural
rules, including for command profiles; they do not promise that a future compiler
supports every structurally valid provider pair.

## Budget scopes (V2-001A correction)

Profiles describe provider/model capabilities; roles describe WHO via routing, effort
and escalation. States describe WHAT work occurs and its local limits. A reusable
role is not an accounting scope. V2-001's shared-role interpretation could cause
VERIFY to consume the allowance needed by VERIFY_AGAIN merely because both select
`verifier`. That coupling was unintended: **all role-level budget mappings are now
rejected**, including max_total_invocations and max_escalations. Shared named scopes
would require a separate explicit future contract.

| Location | Accepted fields | Exact declared scope |
|---|---|---|
| Primary/escalation rung `budget` | **max_invocations** | Calls to this rung of this logical state across all its visits in one run |
| Normal state `budget` | `max_invocations`, `max_state_visits` (at least one), required **on_exceeded** | Invocations across all its rungs; logical entries into this state, respectively, within one run |
| Flow `run_budget` | **max_invocations**, **on_exceeded** | All executor invocations across the entire run |

All numeric limits must be positive integers, excluding bool, floats, strings, zero, negatives
and null. Supplied mappings cannot be empty. Omitted state limits are unspecified
(`None`), subject to the mandatory run invocation cap. Omitted rung budgets normalize
to `BudgetSpec(max_invocations=1)`: one invocation for that logical state's rung per
run, never a shared counter across states using the role. An explicit larger limit
permits more attributable work across visits; it does not introduce implicit retries.
Limits are independent upper bounds, not reservations or guarantees of execution.

A logical entry occurs at the initial state or an explicit workflow transition,
including a self-transition. Escalation into another rung does not constitute another
logical entry. A terminal has no local budget and ends the run.

V2-001B intentionally removes `run_budget.max_state_visits` instead of implementing
another runtime counter. The strict loader rejects it, even alongside a valid
`max_invocations`. The mandatory run invocation cap bounds executable work; state-local
visit limits bound repetition where specified, and static runtime cycle validation
prevents refusal loops. State-local `max_state_visits` remains available. No whole-run
visit, role, duration, cost or token budget is part of this DSL.

`max_duration_seconds` is rejected at every location. Timing observability remains
unchanged; measuring durations is not deadline enforcement. Exact deadline/cancellation
semantics need separate future work. No declared duration is silently ignored.

### Explicit exhaustion ownership (V2-001C)

Flow `run_budget.on_exceeded` is required and must name an existing terminal.
State `budget.on_exceeded` is required whenever a state budget is supplied and may
name an existing terminal or executable state. A state budget needs at least one
numeric limit; the route alone is insufficient. Terminal states cannot declare budgets.
Rung budgets accept only `max_invocations`, never `on_exceeded`. Both run and state
budget targets participate in reachability; no target or implicit default is invented.

```yaml
run_budget: {max_invocations: 12, on_exceeded: NEEDS_HUMAN}
# Within a normal state:
budget: {max_invocations: 3, max_state_visits: 1, on_exceeded: NEEDS_HUMAN}
escalation_exhausted: NEEDS_REDIAGNOSIS
```

The causes remain distinct even if destinations coincide:

- Run invocation exhaustion selects the flow's terminal safety route.
- Logical-state invocation or entry-visit exhaustion selects the state budget route;
  it must not select another rung as a way to bypass refused admission.
- Rung invocation exhaustion may select the immediately next rung if its incoming
  triggers contain `budget_exceeded`; otherwise normal `on.budget_exceeded` applies
  if explicitly present, then `escalation_exhausted` for a ladder-bearing state.
  Non-ladder unhandled refusal is an error. Never skip forward through the ladder.
- Semantic ladder exhaustion continues to use `escalation_exhausted`, independently
  of the enclosing state's capacity and route.

Cause precedence preserves existing runtime order: entry visit limit first, run
invocation limit second, then scopes. The future compiler must declare the shared
logical-state invocation scope before the individual rung scope. If both scopes
are exhausted, the enclosing state route wins. This slice does not change runtime.

RUNTIME-V2-D supplies cause-aware refusal routing for run invocation, state visits,
and exact named invocation scopes, with legacy fallback. RUNTIME-V2-E commits visits
before invocation checks, so invocation-refused logical entries remain counted.
Runtime must not learn logical-state, role, rung, escalation or provider semantics.
The loader remains declarative; opt-in compilation is described below.

## Exhaustion and matching

Every normal state using a role with a nonempty ladder **must** declare
`escalation_exhausted: <existing-state>`. Terminals and non-ladder states forbid this
field. The destination may be executable or terminal and is included in reachability.
There is no globally chosen human/escalation destination.

The declarative dispatch order for a reported outcome is:

1. Only the immediately next rung's incoming `on` list can select an escalation.
   When it explicitly matches and admission permits, it takes precedence over normal on.
2. Otherwise use the logical state's normal `on` mapping if it contains the outcome.
3. Otherwise, for a recognized escalation trigger in a ladder-bearing state, use
   `escalation_exhausted`. This covers a final rung or a next rung that does not match.
4. Unknown/unhandled outcomes outside that case are errors, not a fallback success.

For example, if rung 1 accepts only inconclusive and rung 2 accepts diagnosis_disproved,
a primary diagnosis_disproved never skips to rung 2. A normal diagnosis_disproved edge
wins if present; otherwise the explicit exhaustion edge applies. Semantic labels such
as ready, reproduced, not_reproduced, needs_human, pass and fail remain unchanged strings.

`budget_exceeded` remains a control-plane fact, not a provider-returned result. The
routing order does not override admission: an exhausted state/run cap cannot authorize
additional work by escalating. RUNTIME-V2-B/C provide generic refusal routing and
admission; the V2 loader does not execute any of these declarations.

## Graph and YAML validation

Entry and transition targets must exist within their own flow. All states must be
reachable from entry through normal transitions, explicit escalation exhaustion or budget exhaustion edges. Terminal states have no role, transitions or artifact fields;
normal states require a known role and transitions. `terminal: false` is not accepted;
normal states omit that key. A terminal-only flow is valid if it still has the required
run budget. No static requirement for a path to a terminal is imposed beyond reachability.

The existing strict parser rejects duplicate keys, non-string mapping keys, anchors,
aliases, explicit tags, merge keys and multiple documents. Bare `on` is a string key;
only lowercase `true`/`false` are implicit booleans. Standard scalar typing is preserved
and schema validation never coerces the result.

## Public values and boundaries

Frozen dataclasses: `WorkflowBundle`, `ArtifactSpec`, `ExecutionProfile`, `RoleBinding`,
`InvocationBinding`, `EscalationStep`, `BudgetSpec`, `WorkflowSpec`, `WorkflowStateSpec`.
Mappings are defensively copied into read-only mappings. Artifact lists, triggers and
ladders become tuples. Missing state budgets/effort/models use `None` where allowed; missing rung budgets default to one invocation.
Direct constructors provide immutable data containers; `load_workflow_bundle` is the
configuration validation boundary. There is no mutable builder, registry or plugin API.

The loader does not execute workflows. Use V2-002 compilation before `runtime.run()`;
the v1 CLI does not accept V2 bundles. V2-003A storage and future context transport
remain separate from loader validation.

## Compilation API (V2-002)

`switchyard.workflow_compiler.compile_workflow(bundle, flow_name, executor_resolver)`
returns a frozen `CompiledWorkflow` with `flow`, read-only `executors`, `plan`, and
`entry_states`. Execute it with the existing `runtime.run`; compilation never invokes
an executor. Existing V1 and delivery presets do not pass through this compiler.

The resolver is a callable `ExecutionBinding -> Executor`, called once per executable
compiled state during compilation, sorted by logical state then ascending rung index.
Each frozen binding contains `flow_name`, `logical_state`, `role_name`, `rung_index`,
`profile_name`, `provider`, `model`, `effort`, `escalation_kind`, and `context_mode`.
Primary kind/context are `None`; later bindings preserve `vertical`/`continue` or
`horizontal`/`fresh`. Effort is copied verbatim; command effort remains `None`.
The compiler has no provider-specific branches or adapter construction.

`plan[runtime_state_id]` is a frozen `CompiledStatePlan` containing:

- `logical_state` and `binding` (None for terminal states).
- `declaration`: immutable source state, including logical transitions, budgets,
  `consumes`, and `produces`.
- `state`: the actual runtime State with compiled transitions, scope order and routes.

`entry_states[logical_name]` identifies each logical entry. Terminal IDs preserve
logical names, so `RunResult.terminal_state` is directly usable. Executable IDs and
scope names use separate deterministic compiler namespaces that cannot collide with
validated logical identifiers. Their formatting is private; use the mappings rather
than splitting generated strings. Role reuse never shares state or scope identity.

The compiler encodes adjacent escalation, normal transitions and exhaustion directly
in `State.on`. `budget_exceeded` never becomes a semantic executor outcome; the same
precedence selects only the rung scope's refusal destination. Run refusal uses the
flow route, visit refusal uses the entry state's route, and shared invocation refusal
uses the enclosing scope route. Shared scope precedes local scope. Only entry states
carry logical visit caps; default rung invocation caps are retained.

There are no synthetic executor calls, counters or execution loops in the compiler.
Artifact declarations and context policies are inspection metadata only. Executors
receive the existing InvocationRequest; no artifact data or chat history is fabricated.

`WorkflowCompileError` identifies resolver failures (with the original cause), invalid
resolved executors, generated identifier collisions and runtime representation errors.
Unknown flow selection retains `WorkflowBundle.flow`'s `FlowConfigError`. The compiler
assumes loader-validated input and does not duplicate the schema validator.

The existing conservative runtime cycle validator remains authoritative. A loader-valid
cyclic graph that lacks the visit bounds required by runtime fails compilation explicitly;
the compiler neither invents additional limits nor relaxes cycle safety. This is a
representation constraint, not silent approximation. Real provider resolution and
artifact/context transport remain future slices.

## Artifact catalog (V2-003A)

The optional root `artifacts` mapping defines frozen `ArtifactSpec` values with required
`format` (markdown/text/yaml/json), optional `schema: microspec` (YAML/JSON only), and
optional `source: request`. A nonempty catalog requires the immutable source named
`request`, with markdown/text format and no schema. Consumes/produces references must
be declared; producing the request is forbidden. Empty/absent catalogs remain valid
for artifact-free callers. Declared produces do not force publication on every outcome.

`CompiledWorkflow.artifacts` preserves these definitions without altering execution.
The caller-owned workspace, strict manifest, MicroSpec validator and immutable handoffs
are documented in [the durable work contract](work-contract.md). No executor/context
transport is implemented in this slice.


## Declarative command capabilities (V2-001D)

A profile is the physical execution capability. States still choose roles, and roles
choose profiles. The portable command definition belongs in the bundle; machine-local
executable resolution, working directory and transport timeout remain external.

```yaml
profiles:
  verifier:
    provider: command
    command:
      argv: [python3, -m, unittest, discover, -s, tests]
      outcomes:
        exit_codes: {0: pass, 2: blocked}
        default: fail
      capture:
        artifact: verifier_evidence
        stream: combined
        max_bytes: 32768
```

`command` requires exactly `argv` and `outcomes`, with optional `capture`. Unknown
keys are rejected throughout. Command profiles reject model and profile effort;
command invocation bindings also continue to reject effort. Agent profiles reject
command. Existing bare command profiles must now declare this capability explicitly.

`argv` is a nonempty list of exact nonempty strings without NUL, copied to a tuple.
It specifies one direct process invocation, never a shell command string. Relative
paths and tool names are allowed; the loader neither executes nor resolves them.
Absolute paths are not required; portable committed bundles should avoid local paths.
Arguments are literal: no substitution/interpolation occurs, including braces or
shell metacharacters. No env, command chaining, retries, extraction regexes or template
language are supported. Multiple checks belong in one project wrapper or existing
verification entrypoint. The canonical Switchyard example reuses its documented
`python3 -m unittest discover -s tests` entrypoint, not an Adeo command.

`outcomes` requires `exit_codes` (a mapping, possibly empty) and `default`. Keys are
nonnegative integers, excluding bool and quoted numeric strings. There is no platform
exit-code upper bound imposed by the portable schema. Negative process return codes
can represent signal termination and are not normal exit statuses. Values and default
use the existing V2 outcome identifier syntax. `budget_exceeded` is forbidden.
The strict parser accepts integer keys only at this specific V2 mapping path; V1
and other mapping-key rules remain unchanged, including duplicate-key rejection.

An explicit code mapping wins; default handles every other **normal process exit**.
Missing executable, spawn failure, timeout, cancellation and signal/OS transport
failures are execution failures, not default semantic outcomes. The command adapter
must not turn them into `fail` just because that is the configured default.

Every possible configured outcome is validated for every state using the profile,
including profiles in later escalation rungs. Validation follows ordinary precedence:
next adjacent rung trigger, normal state transition, then recognized trigger with an
explicit escalation-exhaustion destination. No forward rung search or command-specific
routing is introduced. Unhandled outcomes fail bundle loading.

Capture, when present, requires all three fields. `artifact` must name a declared,
non-request text/markdown artifact without a schema; each using state must list it
in produces. YAML/JSON/MicroSpec raw captures are invalid. `stream` is exactly stdout,
stderr or combined. `max_bytes` is a positive integer excluding bool. Combined selects
both streams; V2-004 must define the exact combination order/format.

Capture is produced on every normal exit, including pass and fail. Evidence uses the
existing versioned stream: VERIFY and VERIFY_AGAIN both produce verifier_evidence,
so later consumers naturally select its latest committed version.

The future capture representation must retain a UTF-8 tail within max_bytes and
include truncation status **inside** that byte bound. Reserve the first ASCII byte
as a status marker: `=` for complete output, `!` for truncated output. The remaining
max_bytes - 1 bytes contain complete UTF-8 characters from the tail. Thus even a
one-byte limit can explicitly report truncation. Normalize invalid UTF-8 with
replacement before selecting the bounded tail; do not split encoded characters.
These are declarative requirements only; no capture code is implemented in this slice.

Frozen types are `CommandSpec(argv, outcomes, capture=None)`,
`CommandOutcomes(exit_codes, default)` and `CommandCapture(artifact, stream, max_bytes)`.
`ExecutionProfile.command` contains this capability for command profiles and is None
otherwise. CommandOutcomes copies its map to a read-only mapping.

`CompiledWorkflow.profiles` preserves the immutable bundle profile catalog.
`ExecutionBinding` remains unchanged: its profile_name identifies the definition.
A future provider resolver can receive `bundle.profiles` during setup (before
compilation calls it), while plan inspection uses `compiled.profiles`. Neither needs
to reread YAML or infer commands from role/state names. No command fields enter runtime
State, no process is launched by loading/compilation, and no adapter is implemented.

### Optional per-rung return restrictions (V2-001E)

Primary bindings and escalation bindings may declare `outcomes: [name, ...]`.
**Escalation `on` controls entry into a rung from its immediate predecessor;
`outcomes` controls what that rung's executor may return.** For example:

```yaml
primary:
  profile: claude-haiku
  effort: medium
  outcomes: [inconclusive]
escalation:
  - kind: vertical
    on: [inconclusive]
    profile: claude-sonnet
    effort: high
    context: continue
    outcomes: [diagnosis_disproved]
  - kind: horizontal
    on: [diagnosis_disproved]
    profile: codex
    effort: high
    context: fresh
    outcomes: [ready]
```

The list is optional, nonempty, unique, and uses the existing semantic identifier
syntax. Strings are not coerced; `budget_exceeded` is forbidden. Omission preserves
all existing outcomes and routing. Command bindings may restrict outcomes only if
all explicit exit-code mappings and the default outcome remain included.

The compiler first builds the existing full semantic transition map: adjacent
rung match, otherwise normal state transition, otherwise recognized escalation
trigger to `escalation_exhausted`. It rejects any explicit outcome absent from that
map, separately for every state using the role, then filters the map before
constructing the runtime State. A restriction cannot invent a destination or skip
a rung. Final rungs may still return recognized triggers that route to exhaustion.
Empty explicit contracts are rejected.

All budget routes remain independent and unchanged. Runtime itself is unchanged:
its existing outcome validation enforces the narrowed State transitions.
`executor_allowed_outcomes`, StageOutputContract, and the shared provider output
schema all derive from this same compiled contract. Canonical delivery workflows
omit restrictions and retain their original behavior.
