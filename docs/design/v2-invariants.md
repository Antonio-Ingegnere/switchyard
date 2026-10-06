# Switchyard V2 invariants

These rules govern bounded, single-work-item V2 delivery. Historical slice notes
explain evolution; this document states the current contract.

## Runtime and configuration

- Agents execute; runtime owns transitions. A bundle contains named flows; a
  logical state uses a role whose profiles bind individual execution rungs.
- Undeclared semantic outcomes fail. Control-plane `budget_exceeded` is not a
  provider outcome. Budgets change control flow and bound every cycle.
- Escalation is adjacent: no rung skipping. Vertical/horizontal describe the
  configured ladder, not an adapter's autonomous fallback.
- Profiles select provider/model configuration; states declare capabilities.
  Unsupported provider/capability combinations fail during resolution, before
  any invocation. Omitted legacy capability policy differs from explicit `[]`.

## Context and provider boundaries

- Durable facts come from verified artifact snapshots. A handoff is prior
  reasoning, not repository truth.
- `continue` can include exactly the selected preceding structured handoff.
  `fresh` excludes it but retains declared durable artifacts.
- Provider sessions are never workflow authority. Continuation is Switchyard
  context construction, not native conversation resumption.
- Provider transport adaptation never weakens canonical contracts. No automatic
  workflow retry, model substitution, artifact coercion or handoff repair occurs.

## Output and verification

- Canonical StageResult has strict outcome/artifacts/handoff/diagnostics shape.
  Context transport checks the compiled outcome contract and validates every
  artifact and handoff before publishing any invocation output.
- Invalid outputs leave no committed invocation output. This validation guarantee
  is not an I/O transaction: interruption during sequential publication can leave
  a committed prefix. Preserve it and stop; do not silently replay.
- Deterministic verification is preferred. Command exit mappings are separate
  from model semantic outcomes; command evidence is persisted normally.

## Human QA and Git

- `READY_FOR_MANUAL_QA` ends delivery. The caller's explicit human decision starts
  a separate accept/reopen operation; models do not choose acceptance.
- A QA candidate captures base HEAD, target identity, exact changed paths and
  reviewed bytes/modes/deletions. Acceptance owns the deterministic final commit.
- Unsafe or stale work fails closed as `COMMIT_BLOCKED`. No cleanup, unrelated
  staging, history rewrite or push. A post-publication failure never claims rollback.
- `REOPENED` preserves work and records human feedback, without invoking a model.
- Completed decisions finalize the candidate. Unresolved intent requires manual
  inspection; replay must not produce a second commit.

## Durability

- Correctness is reconstructible from workflow, request, artifacts, handoffs,
  manifest, recorded events and lifecycle records—not hidden provider memory.
- Completed-run reconstruction is not resumption of an interrupted runtime.
  Integrity errors, partial writes and stale locks fail explicitly. See the
  [failure/restart audit](v2-readiness.md#failure-and-restart-boundaries).
