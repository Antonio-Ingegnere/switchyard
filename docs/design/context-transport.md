# Artifact-aware V2 execution

`switchyard.context_transport.workspace_resolver(bundle, flow_name, workspace,
stage_executor_resolver, *, max_context_bytes=65536)` composes with
`compile_workflow`. The catalog must equal the workspace catalog. Use the same
bundle and selected flow for both calls. Each binding resolves once during compilation.
Runtime is unchanged; provider adapters render the same generic stage contract.

The delegate is a callable receiving `ExecutionBinding` and returning a
`StageExecutor` with `run(StageInvocation) -> StageResult`.

Frozen transport values:

- `StageInvocation(binding, artifacts, previous_handoff=None, diagnostics=None, *, output_contract)`.
- `ArtifactSnapshot(record: ArtifactRecord, content: str)`: record supplies logical
  name, explicit version, format/schema, SHA-256, byte size and provenance.
- `ProducedArtifact(name, content)`: format and schema are owned by the catalog.
- `StageHandoff(summary, findings=(), evidence=(), changed_assumptions=(), blockers=())`.
- `StageResult(outcome, produced_artifacts=(), handoff=None, diagnostics=None)`.

Collections are defensively copied to tuples. Diagnostics use the existing
`DiagnosticInput` and `DiagnosticPayload` types unchanged. `ContextualExecutor`
implements runtime Executor and holds binding, state declaration, workspace,
stage executor, immutable output contract and context byte limit. `WorkContractError` reports invalid context
or outputs, prefixed with flow/state/rung; runtime retains its normal executor-error boundary.

## Input selection

Before stage execution, verify the immutable request exists and exactly equals
WorkUnit.task whenever the catalog declares a request source. Request consistency
is checked even when request is not consumed; its text is not implicitly injected.

Read exactly `consumes` in declaration order, using latest committed versions and
verified SHA-256/size. Capture immutable content snapshots, not just file paths.
Missing or altered inputs fail before calling the stage. Separate work roots have
independent manifests and no shared context cache.

Primary invocations receive no historical handoff. Fresh invocations also receive
none, but keep all declared factual artifacts. Continue selects exactly the latest
committed handoff with the same flow/state and immediately preceding rung index.
It may originate from an earlier logical visit: run-scoped rung budgets can refuse
a later primary invocation. A missing predecessor fails explicitly.

Handoffs are prior reasoning, not authoritative facts. Continue does not mean a
provider-native session. Fresh never promotes old handoff prose into evidence.
Declared artifacts remain workflow-owned: declaring a reasoning artifact in
consumes explicitly includes it, even for fresh.

The entire structured input (binding, snapshots, optional handoff and diagnostics)
has a default 65536-byte bound. Size is measured using compact sorted-key UTF-8
JSON with decoded diagnostic bytes. This is accounting, not provider prompt
formatting. Oversized context is rejected, never truncated. Selection is bounded;
workspace integrity reads may still inspect historical versions.

## Output validation and persistence

Validate result type, nonempty string outcome (no NUL/reserved budget_exceeded),
diagnostics type, all produced names/content/schema and the complete handoff before
writing anything. Duplicate or undeclared production is invalid; request cannot be
produced. MicroSpecs use the existing validator. Omitted declared outputs are legal;
a subsequent consumer enforces availability.

The wrapper assigns handoff provenance from the binding, outcome from StageResult,
and produced_artifacts from validated production. Workspace assigns sequence.
StageHandoff cannot supply these fields. `WorkWorkspace.validate_artifact` and
`validate_handoff` provide the same storage validators without writes.

Commit artifacts in result order, then the handoff, then return InvocationResponse.
No runtime transition is selected by this layer. Runtime remains authoritative for
state-declared outcome membership; an undeclared semantic outcome can consequently
be rejected after durable outputs have committed. Storage success is not execution
success and must be interpreted with canonical runtime events.

Prevalidation prevents validation-induced partial batches. I/O failure can still
leave a committed prefix; raise before returning an outcome and retain immutable
versions. There is no transaction, rollback or automatic retry. Existing workspace
crash/orphan rules apply. Cooperating execution should serialize a workspace; this
API does not claim a multi-artifact snapshot transaction across concurrent writers.

No real providers, provider prompts, chat transcripts or session state are stored
or constructed here.

## Explicit stage output contract (V2-003C)

Every StageInvocation requires an immutable `output_contract`:

- `StageOutputContract(allowed_outcomes, artifacts, handoff)`.
- `StageOutputArtifactContract(name, format, schema=None, schema_fields=())`.
- `StageHandoffContract(expected, evidence_artifacts, supported=True)`; its immutable
  `body_fields` derives from StageHandoff itself.

The compiler copies sorted keys from each generated runtime State.on, excluding
control-plane `budget_exceeded`, into `ExecutionBinding.executor_allowed_outcomes`.
CompiledStatePlan exposes the same property. It does this before eager executor
resolution; no circular dependency, registry, routing reconstruction or runtime-name
parsing is involved. ContextualExecutor consumes that tuple without learning targets.
Current ladder rungs happen to have equal sets because exhaustion handles every
recognized escalation trigger; this slice does not change that routing semantics.

Produced descriptors come from state.produces plus the bundle artifact catalog.
Names are exact logical identifiers, never filenames. All production remains optional;
only later consumers require availability. `schema_fields` for microspec uses the
same frozen `MICROSPEC_FIELDS` descriptions as validate_microspec: id/goal are nonempty
strings; scope/constraints/acceptance/verification/non_goals are lists of nonempty
strings (empty lists allowed). All seven fields are required; existing extension-field
acceptance is unchanged. YAML/JSON is raw content, without markdown fences.

The shared renderer also adds one `MICROSPEC CONTENT RULES (<format>)` section per
microspec artifact format in the contract, generated by `microspec_guidance` from the
same `MICROSPEC_FIELDS` (V2-003D). It states each required key, that every list item
is one non-empty scalar string (never a mapping, nested sequence, null, number or
boolean) and, for YAML, to double-quote every list item: an unquoted item containing
`": "` parses as a mapping (`- task_queue.py: preserve FIFO` is a mapping;
`- "task_queue.py: preserve FIFO"` is a string). Guidance teaches serialization of
the existing contract; the validator still rejects mappings and never stringifies.

Handoffs are supported and advisory-expected for model stages, optional for commands.
Body fields are summary/findings/evidence/changed_assumptions/blockers; provenance,
sequence, outcome and produced_artifacts remain wrapper/store-owned. Evidence names
come from the declared catalog; repository paths belong in findings or summary.

Output contract JSON has a separate fixed 65536-byte ceiling. It is excluded from
max_context_bytes, as is the newly added outcome tuple on binding, preserving previous
durable input accounting exactly. No arbitrary schema plugins exist. Oversized output
contracts fail during construction; they cannot create unbounded framework guidance.

Post-result validation and commit ordering are unchanged. A returned spec.md still
fails when only microspec is declared; guidance never aliases or repairs model output.

### Per-rung restrictions

V2-001E optionally narrows primary/escalation return outcomes in the compiler.
`StageOutputContract.allowed_outcomes` continues to derive from the actual compiled
runtime State through `ExecutionBinding.executor_allowed_outcomes`; context
transport adds no override or routing logic. Consequently a three-rung contract can
expose exactly `(inconclusive,)`, `(diagnosis_disproved,)`, and `(ready,)`, and the
existing Claude/Codex schema generator receives those same enums. Budget refusal
routes never appear as executor-returnable `budget_exceeded`. Durable input
selection, continuation handoffs, fresh exclusion, and output validation are
unchanged.

### Explicit handoff body guidance (V2-003E)

The renderer adds `HANDOFF BODY RULES` from the work-contract's shared body field
metadata and size bounds. It describes each required non-null field, prose versus
identifier content, empty-list allowance, UTF-8/non-whitespace text and limits.
`evidence` is a list of strings, not nested reference objects. The whole handoff
remains nullable; supported/expected policies are unchanged.

The renderer lists `output_contract.handoff.evidence_artifacts` dynamically. This
is the existing full declared catalog, not just consumed or committed artifacts.
It does not narrow membership or claim availability. ASCII identifier syntax is
explicit; filenames, paths, sentences, space-separated labels and Markdown
references belong in prose fields, not in evidence identifiers. No fixed examples
can accidentally imply permission to reference undeclared names.

All returned artifacts and the handoff are still prevalidated before publication.
An invalid reference now reports `handoff.evidence[index]` with a safe category;
no body value appears in the exception chain. Fresh/continue selection, output
contracts, schema shape, persistence formats and runtime behavior are unchanged.
