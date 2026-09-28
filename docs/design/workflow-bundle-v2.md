# Workflow bundle v2 (corrected by V2-001A)

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
| Root | **version: 2**, **profiles**, **roles**, **flows** |
| `profiles.<name>` | **provider**, **model** for agent providers |
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
No trimming, string/number coercion, machine executable, credential or secret fields
are supported. Model identifiers are not checked against live provider catalogs.

Providers are closed to `claude`, `codex`, and `command` for this first schema.
Agent profiles require a model; command profiles must omit it, including null.
`human` is not a provider. Human boundaries are ordinary project terminal states.
Profiles cannot contain effort, roles, workflow semantics or budgets.

Invocation effort, when supplied, is exactly `low`, `medium`, or `high`. Omission
is represented by `None` and leaves the choice unspecified for future compilation;
there is no default translation here. Command bindings must omit effort, even null.
Roles refer to profiles; states refer only to roles. Unused profiles/roles are allowed
but still fully validated. Declaration order is retained in immutable mappings.

`consumes` and `produces` are lists, defaulting to empty tuples. Every item is a
logical identifier; duplicates within either list are rejected. The same artifact
may appear in both lists or across multiple states. External inputs need not have a
producer in the bundle. Availability, physical storage, lineage, freshness and content
validation are deferred. No filesystem probes occur for artifact declarations.

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
| Normal state `budget` | `max_invocations`, `max_state_visits` (at least one) | Invocations across all its rungs; logical entries into this state, respectively, within one run |
| Flow `run_budget` | **max_invocations**, optional `max_state_visits` | All executor invocations; all logical state entries across the entire run, respectively |

All values must be positive integers, excluding bool, floats, strings, zero, negatives
and null. Supplied mappings cannot be empty. Omitted state limits are unspecified
(`None`), subject to the mandatory run invocation cap. Omitted rung budgets normalize
to `BudgetSpec(max_invocations=1)`: one invocation for that logical state's rung per
run, never a shared counter across states using the role. An explicit larger limit
permits more attributable work across visits; it does not introduce implicit retries.
Limits are independent upper bounds, not reservations or guarantees of execution.

A logical entry occurs at the initial state or an explicit workflow transition,
including a self-transition. Escalation into another rung does not constitute another
logical entry. Whole-run visits include a reached terminal. A terminal has no local
budget and ends the run. Run max_state_visits is optional: each nonterminal entry
requires work, and the mandatory invocation fuse bounds execution; it is useful as
an additional global admission fuse. It no longer means a cap repeated per state.

`max_duration_seconds` is rejected at every location. Timing observability remains
unchanged; measuring durations is not deadline enforcement. Exact deadline/cancellation
semantics need separate future work. No declared duration is silently ignored.

This remains schema only. Invocation/visit admission, refusal precedence and enforcement
are the RUNTIME-V2-C prerequisite; no runtime enforcement is claimed here. No provider
or role semantics need to enter runtime. Existing version-1 budgets are unchanged.

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
additional work by escalating. RUNTIME-V2-B/C must define refusal scope, admission and
safe nonterminal budget routing; V2-001A does not execute any of these declarations.

## Graph and YAML validation

Entry and transition targets must exist within their own flow. All states must be
reachable from entry through normal transitions or explicit exhaustion edges. Terminal states have no role, transitions or artifact fields;
normal states require a known role and transitions. `terminal: false` is not accepted;
normal states omit that key. A terminal-only flow is valid if it still has the required
run budget. No static requirement for a path to a terminal is imposed beyond reachability.

The existing strict parser rejects duplicate keys, non-string mapping keys, anchors,
aliases, explicit tags, merge keys and multiple documents. Bare `on` is a string key;
only lowercase `true`/`false` are implicit booleans. Standard scalar typing is preserved
and schema validation never coerces the result.

## Public values and boundaries

Frozen dataclasses: `WorkflowBundle`, `ExecutionProfile`, `RoleBinding`,
`InvocationBinding`, `EscalationStep`, `BudgetSpec`, `WorkflowSpec`, `WorkflowStateSpec`.
Mappings are defensively copied into read-only mappings. Artifact lists, triggers and
ladders become tuples. Missing state budgets/effort/models use `None` where allowed; missing rung budgets default to one invocation.
Direct constructors provide immutable data containers; `load_workflow_bundle` is the
configuration validation boundary. There is no mutable builder, registry or plugin API.

V2-002 compilation/routing and V2-003 artifact/context behavior are outside this slice.
Do not pass these specifications directly to `runtime.run()` or the v1 CLI.
