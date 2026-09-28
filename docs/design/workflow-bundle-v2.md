# Workflow bundle v2 (V2-001)

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
| `roles.<name>` | **primary**, `escalation`, `budget` |
| `primary` | **profile**, `effort`, `budget` |
| Each `escalation` item | **kind**, **on**, **profile**, **context**, `effort`, `budget` |
| `flows.<name>` | **entry**, **run_budget**, **states** |
| Normal state | **role**, **on**, `consumes`, `produces` |
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

## Budget scopes

Every supplied limit must be a strictly positive integer. `true`, zero, negatives,
float values, strings and null are rejected. A supplied budget mapping cannot be empty.
Absent local budgets are `None` (unspecified), not zero or an invented default.

| Location | Accepted fields | Declared scope |
|---|---|---|
| Primary/escalation rung `budget` | `max_invocations`, `max_duration_seconds` | That rung's usage for that role in one run |
| Role `budget` | `max_total_invocations`, `max_escalations`, `max_duration_seconds` | All uses of that role in one run |
| Flow `run_budget` | **required `max_invocations`**, `max_state_visits`, `max_duration_seconds` | Entire run; `max_state_visits` caps each named state's visits within that run |

Duration fields declare elapsed-time caps for their respective scope, with aggregation
and enforcement deferred to the compiler/runtime work. No timers or budget counters
are added here. Escalation is an ordered ladder: `max_escalations`, when supplied,
cannot exceed ladder length; it also cannot exceed `max_total_invocations - 1` when
that role total is supplied (primary plus escalation invocations). A smaller escalation
cap may intentionally cut off the ladder. No role without a ladder can specify a
positive escalation cap. Omit that field when escalation is absent.

Other inner budgets may exceed an outer fuse. The validator does not promise that
all rungs, retries or paths can execute before the hard run cap. It does not estimate
path costs or prove termination. Cycles are permitted with the mandatory run cap.
No run is executed and no existing runtime budget semantics change in V2-001.

The canonical example uses verifier and fixer totals of two: bugfix can visit both
VERIFY/VERIFY_AGAIN and FIX/REFIX. These are per-role totals, not reset per state.
The feature and bugfix graphs have separate entries/states and do not continue each
other. All provider and role choices live only in the example YAML.

## Graph and YAML validation

Entry and transition targets must exist within their own flow. All states must be
reachable from entry. Terminal states have no role, transitions or artifact fields;
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
ladders become tuples. Missing local budgets/effort/models use `None` where allowed.
Direct constructors provide immutable data containers; `load_workflow_bundle` is the
configuration validation boundary. There is no mutable builder, registry or plugin API.

V2-002 compilation/routing and V2-003 artifact/context behavior are outside this slice.
Do not pass these specifications directly to `runtime.run()` or the v1 CLI.
