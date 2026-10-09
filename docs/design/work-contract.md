# Durable work contract (V2-003A)

Agent memory is never the handoff contract. The original request, versioned artifacts,
and structured handoffs live in a caller-owned workspace. This layer does not build
prompts, select fresh/continue context, wrap executors or call providers.

## Artifact catalog

Workflow Bundle V2 accepts an optional top-level `artifacts` mapping:

```yaml
artifacts:
  request: {format: markdown, source: request}
  microspec: {format: yaml, schema: microspec}
  reproduction_evidence: {format: markdown}
  diagnosis: {format: text}
  fix_microspec: {format: json, schema: microspec}
```

Each definition has required `format` (`markdown`, `text`, `yaml`, `json`), optional
`schema` (only `microspec`, with YAML/JSON), and optional `source` (only `request`).
A nonempty catalog requires exactly one request source named `request`; it must use
markdown/text without a schema. No other name can use that source. Names use V2's
`[A-Za-z][A-Za-z0-9_.-]*` lexical contract, excluding paths, NUL, `.` and `..`.
Unknown fields, invalid types and undeclared consumes/produces references fail loading.
States cannot produce the immutable request.

Omitted/empty catalogs remain valid for artifact-free flows. Existing flows with
artifact references must declare them; missing definitions are not inferred. Produces
is a declaration, not an unconditional requirement to publish on every outcome.
Availability checks and selecting immutable versions for consumption belong to V2-003B.

`WorkflowBundle.artifacts` and `CompiledWorkflow.artifacts` are read-only mappings of
frozen `ArtifactSpec(format, schema=None, source=None)` values. State plan declarations
still expose consumes/produces. Catalog changes do not change the compiled runtime graph.
The canonical bundle declares the feature and bugfix contracts, including future bug/fix
MicroSpecs, without changing its transitions, state budgets or existing artifact names.

## Workspace API

```python
from switchyard.workflow_bundle import load_workflow_bundle
from switchyard.work_contract import WorkWorkspace, ArtifactProducer

bundle = load_workflow_bundle("examples/delivery/workflows-v2.yaml")
workspace = WorkWorkspace.open(work_root, bundle.artifacts)
workspace.initialize_request(original_request)

record = workspace.write_artifact(
    "diagnosis", "Observed focus behavior and current diagnosis.\n",
    producer=ArtifactProducer("bugfix", "DIAGNOSE", 0, "claude-haiku"),
)
text = workspace.read_artifact("diagnosis", version=record.version)
versions = workspace.versions("diagnosis")
latest = workspace.latest_artifact("diagnosis")

# The manifest persists the catalog; reopening need not supply it again.
workspace = WorkWorkspace.open(work_root)
```

A new workspace requires a catalog. If supplied on reopen, it must equal the persisted
catalog. There is no global directory convention or absolute machine path in records.
The layout is:

```text
<work-root>/
    request.md
    artifacts/<logical-name>/000001.md   # extension follows declared format
    artifacts/<logical-name>/000002.md
    handoffs/000001.json
    handoffs/000002.json
    manifest.jsonl
```

`request.md` is always the request filename, including `format: text`. It preserves
exact UTF-8 bytes, including line endings and missing final newline. Reinitialization
with identical text is idempotent after hash verification; different text fails.
`write_artifact` cannot write the request. Empty original text remains exact empty text.

`write_artifact` accepts a string, validates its declared format/schema, then returns a
frozen `ArtifactRecord(name, version, path, format, schema, sha256, byte_size, producer)`.
Versions are contiguous per name from 1. There is no timestamp-based ordering or
in-place replacement. `versions` returns an immutable tuple; `latest_artifact` returns
the highest committed version or None. `read_artifact` returns verified text and fails
on missing requested versions. Selected artifact streams are hash/size-verified,
including old versions; reads never silently adopt externally modified contents.

Optional frozen `ArtifactProducer(flow_name, logical_state, rung_index, profile_name)`
records known bounded provenance. None means unknown provenance, not inferred identity.
No prompts, sessions or chat history are recorded. Store errors use `WorkContractError`.

## MicroSpec validation

The authority remains [the MicroSpec protocol](../protocols/micro-spec.md).
`validate_microspec(content, format="yaml")` returns a frozen `MicroSpec` containing
`id`, `goal`, `scope`, `constraints`, `acceptance`, `verification`, and `non_goals`.
The first two must be nonempty strings; the rest must be lists of nonempty strings,
returned as tuples. No coercion or repair is attempted. The existing protocol does
not forbid empty lists, so empty collections are accepted. It also allows extensions:
optional/unknown fields are not rejected or transformed. Original bytes, including
those fields, remain in the artifact; the typed result exposes only required concepts.

Rejections name the narrowest location and the parsed type, never the rejected
value (V2-003D):

```text
MicroSpec root: expected mapping, got sequence
MicroSpec scope: missing required field
MicroSpec goal: expected non-empty string, got null
MicroSpec scope: expected sequence of non-empty strings, got string
MicroSpec scope[1]: expected non-empty string, got mapping
MicroSpec acceptance[0]: expected non-empty string, got empty string
```

Parsed type names are `string`, `empty string` (empty or whitespace-only), `mapping`,
`sequence`, `null`, `boolean`, `integer`, `number`, `timestamp`, `binary` and
`unsupported value`. Workspace and ContextualExecutor context is prefixed as before,
e.g. `bugfix.SPECIFY_FIX[0]: artifact fix_microspec: MicroSpec scope[0]: ...`. YAML
syntax errors keep their line/column-only messages. Acceptance and rejection are
unchanged, and a rejected artifact still commits no version and no handoff.

YAML uses the existing strict parser (duplicate keys, unsafe tags, aliases and merges
rejected). JSON rejects malformed data, duplicate keys and nonstandard NaN/Infinity
literals. Without a schema, structured artifacts need only be format-valid; markdown
and text are opaque UTF-8 strings. No schema plugin registry is introduced.

## Structured handoffs

`HandoffRecord` has required fields `flow`, `logical_state`, `role`, `rung_index`,
`outcome`, and `summary`. `profile_name` is optional. `findings`, `evidence`,
`changed_assumptions`, `blockers`, and `produced_artifacts` default to empty tuples.
`version` is 1; a new record has `sequence=0`, and the store assigns its committed
sequence starting at 1. Returned records are frozen and independently versioned from
logical artifacts. No extra payload fields are allowed on read.

```python
from switchyard.work_contract import HandoffRecord

saved = workspace.write_handoff(HandoffRecord(
    flow="bugfix", logical_state="DIAGNOSE", role="diagnostician",
    rung_index=0, profile_name="claude-haiku", outcome="inconclusive",
    summary="The skip is reproducible; the actual focus owner remains unresolved.",
    evidence=("reproduction_evidence",),
    changed_assumptions=("The visible row may not own the next focus target.",),
))
all_handoffs = workspace.handoffs()
diagnosis_handoffs = workspace.handoffs("DIAGNOSE")
latest = workspace.latest_handoff()
```

Evidence/produced references are declared logical names, not filesystem paths.
Request cannot be produced. These references do not claim availability or pin a
version; future context assembly must resolve and select actual immutable versions.
Each collection permits at most 64 items, each text item and summary/outcome at most
8192 characters. Empty findings/blockers are allowed; fabricated findings are unnecessary.

**A handoff is an agent's bounded reasoning output, not authoritative repository truth.**
Keep it separate from original intent and durable evidence. Artifact persistence alone
also does not establish correctness: deterministic verifier results and repository
inspection remain the evidence for factual claims. No fresh/continue selection or
provider session/hidden reasoning storage is implemented here.

## Integrity, publication and crash boundaries

The authoritative index is strict `manifest.jsonl`. Every record has `record_version=1`.
The first is a catalog record; later records index artifacts or handoffs with canonical
relative paths, explicit versions/sequences, SHA-256 and UTF-8 byte sizes. Artifact
records additionally include format/schema and optional producer identity. Unknown
keys/kinds, inconsistent definitions, noncontiguous sequences, invalid encoding,
malformed JSON and a missing final newline fail explicitly. Files outside the manifest
are never silently adopted as history.

Writers acquire an exclusive `.write.lock`. Content is validated, written to `.pending`,
flushed/fsynced, and published using an atomic no-overwrite hard link on the same
filesystem. Then its manifest line is appended, flushed and fsynced. Existing final
files cannot be replaced. Local filesystem hard-link support is required. Managed
symlinks and non-regular files are rejected. A single cooperating writer is supported;
readers may see the previous committed manifest or an explicit partial-write error.

This is not a transactional database or a power-loss durability guarantee. Directory
fsync is not promised. A crash may leave a lock, pending file, unindexed published file,
or truncated manifest. Further conflicting writes fail; manual inspection/recovery is
required. Nothing repairs, deletes or adopts such leftovers automatically. An ordinary
caught write failure releases its owned lock but leaves incomplete publication evidence.

Hashes detect content disagreement with manifest records; they are not signatures and
do not defend against a party deliberately rewriting both data and manifest. Concurrent
hostile filesystem replacement is outside the cooperating-writer boundary. Missing or
mutated indexed files fail verified reads rather than returning trusted evidence.

## Handoff diagnostics and identifier contract (V2-003E)

The provider body has exactly `summary`, `findings`, `evidence`,
`changed_assumptions`, and `blockers`. In provider JSON all five keys are required
and none is nullable; the whole handoff may be null. `StageHandoff` and
`HandoffRecord` Python constructors retain their empty collection defaults.
`summary` is nonempty UTF-8 prose. The four collections are sequences of nonempty
UTF-8 strings (not reference objects); empty lists are valid. `findings`,
`changed_assumptions`, and `blockers` contain prose. `evidence` contains exact
logical artifact names from the declared catalog. Membership does not require an
artifact to be committed and does not pin a version. Duplicate references remain
allowed. No paths or descriptive labels are interpreted as artifact references.

Storage rejects whitespace-only strings. The existing transport parser accepts
nonempty whitespace strings; downstream work-contract validation still rejects
these. Prose may contain punctuation, paths and NUL; this slice adds no prose
character restriction. All collections have a 64-item limit and all text items,
summary and outcome have an 8192-character limit, unchanged.

A logical identifier starts with an ASCII letter and continues with ASCII letters,
digits, `_`, `-` or `.`. It is case-sensitive. Empty strings, whitespace, NUL,
slashes, backslashes, other punctuation and non-ASCII characters are invalid.
There is no independent identifier length limit; the handoff item limit applies
to references. A filename-shaped string can satisfy the lexical rule but is still
rejected unless it is an exact declared logical artifact name.

The rest of `HandoffRecord` is wrapper/store-owned: `flow`, `logical_state`, `role`
and optional non-null `profile_name` use the same identifier syntax; `rung_index`
is a nonnegative integer; `version` is integer 1; `sequence` is a nonnegative
integer and must be 0 for submission, then match the manifest on read. Booleans
are not integers. `outcome` is nonempty UTF-8 text; runtime membership checks
remain separate. `produced_artifacts` is a sequence of declared names and cannot
reference the immutable request. The executor derives it from validated outputs.
Persisted records require exactly the record fields; missing/extra keys fail.

Errors use code-owned locations and safe categories, never rejected values:

```text
bugfix.TRIAGE[0]: handoff.evidence[1]: expected logical identifier, got invalid format
handoff.evidence[1]: expected declared artifact identifier, got undeclared reference
handoff.findings[2]: expected non-empty string, got mapping
handoff.blockers[1]: expected non-empty string, got empty string
handoff.summary: expected UTF-8 string, got invalid UTF-8
handoff.sequence: expected 0 for a new handoff, got store-assigned sequence
```

The V2-003D parsed-type helper is reused (including tuple sequences).
Format failure is distinct from missing catalog membership and immutable-request
production. Other categories cover missing/unexpected fields, wrong types, empty
text, invalid UTF-8, length limits, numeric range and manifest sequence mismatch.
Original sequence indices are preserved. Encoding exceptions are replaced outside
the exception handler so neither cause nor suppressed context retains rejected text.

`HANDOFF_BODY_FIELDS` supplies only focused body metadata, with a drift check
against `StageHandoff`. Collection/reference fields and existing size constants
are shared by validation and provider guidance. There is no generic schema system.

ContextualExecutor first validates produced artifacts, then the complete handoff,
then commits artifacts in result order, then the handoff. A rejected handoff leaves
all outputs of that invocation uncommitted, including otherwise valid artifacts.
This does not change the documented I/O/crash boundaries above. There is no
stripping, coercion, reference guessing, evidence removal, repair or retry.

## Human QA lifecycle (V2-007)

A delivery run ends at `READY_FOR_MANUAL_QA`. The caller then snapshots the work
presented to the human. Later, the human selects a separate deterministic `accept`
or `reopen` operation. Providers never select that decision or execute its commit.
The APIs in `switchyard.qa_lifecycle` are:

```python
candidate = create_qa_candidate(
    target, delivery.workspace, delivery.result, "bugfix", fresh_lifecycle_root,
)
# After human review, one of:
result = accept_v2_work(target, candidate.workspace)
result = reopen_v2_work(target, candidate.workspace, "Still fails for an empty queue.")
```

Calling accept is the caller's explicit human-decision attestation, not proof of
authenticated identity. No user account information is collected. The application
must expose these operations to its human caller, never as model tools. Feedback
is nonempty UTF-8 human text, at most 65536 bytes; it is not a MicroSpec. No model
invocation is needed for capture, acceptance, reopen or commit wording.

### Durable candidate and decision

Capture requires a `RunResult` with matching `run_started`/`run_finished` provenance
and terminal `READY_FOR_MANUAL_QA`. It creates one `qa_candidate` v1 JSON artifact.
The record contains source flow, source run/correlation identity, source workspace
and request digest, canonical target/git-directory/branch identity, base HEAD,
sorted changed paths and fingerprints. Fingerprints contain raw-file SHA-256 plus
Git executable mode (100644/100755), or a deletion marker. No file contents, `.git`
contents, timestamp, account identity or provider transcript is persisted.
Candidate identity is its immutable artifact SHA-256. Two equal snapshots from the
same source produce equal candidate content; no new random lifecycle ID is needed.

The lifecycle uses a **normal WorkWorkspace**, not a second persistence system.
Its fixed catalog is `request` (immutable copy), `qa_candidate` (JSON), `qa_intent`
(JSON), `qa_decision` (JSON) and `qa_feedback` (text). It must be a fresh directory
outside the target worktree. The delivery workspace's catalog is immutable, so a
separate instance is necessary without changing canonical workflow catalogs or
V2-006 history. Manifest, digest, versioning and crash rules are exactly the existing
WorkWorkspace rules. Source data is referenced; acceptance does not require an
in-process delivery or a provider session.

A lifecycle lock serializes operations for a candidate. An immutable `qa_intent`
version is written before any Git mutation. Accept adds a prepared intent version
with the exact commit object ID before updating the branch. Finally a single
`qa_decision` records candidate identity, human operation, terminal, reason,
committed HEAD (if published), source run/correlation and feedback artifact reference.
Reopen stores `qa_feedback` v1 and returns `REOPENED`: HEAD, index and worktree are
untouched; candidate and source artifacts remain available for later rework. It
intentionally does not require an unchanged Git tree, since it commits nothing.
No repair workflow is launched.

Every completed decision, including `COMMIT_BLOCKED`, finalizes its candidate.
Repeating accept/reopen raises `LifecycleError: candidate already finalized`.
Rework or another review uses a new candidate/workspace. An intent without a final
decision (or a stale lifecycle lock) blocks replay and requires manual inspection.
An audit persistence failure raises `LifecycleRecoveryRequired`, exposing the
known committed HEAD, if any. Inspect the prepared intent and Git refs; never
silently retry. This avoids duplicate commits even if a process dies after Git
publication but before the decision is durable. This is fail-closed recovery,
not an automatic cross-filesystem transaction or exactly-once distributed service.

### Exact Git acceptance boundary

V2 reuses DELIVERY-001's conservative repository guards and immutable-tree/CAS
primitives extracted to `switchyard.git_safety`. DELIVERY-001 retains its existing
manual pre-staging, approval digest, public API, behavior and runtime events.

Capture and accept require a born local branch at the repository root. In-progress
merge, rebase, cherry-pick, revert, bisect or sequencer state, unmerged entries,
submodules, sparse/skip-worktree or assume-unchanged entries block. Changed paths
must be regular files or deletions; symlinks and `.git`/`.switchyard` paths block,
except the exact project configuration path `.switchyard/config.yaml`. RECOVERY-001
allows that file as ordinary fingerprinted QA content so a reviewed timeout retune
can accompany resumed implementation. All runtime state remains protected.
Non-UTF-8 pathnames block. All nonignored untracked files are part of scope;
ignored files are outside the documented Git cleanliness policy and never staged.
The snapshot is checked twice at capture. As with DELIVERY-001, cooperating callers
must provide exclusive worktree access during snapshot/accept; hostile writers
cannot be made transactional by hashing alone.

Before acceptance:

- Canonical target, Git directory and branch must match the candidate.
- HEAD must equal the captured base; no rebase, merge or history repair is attempted.
- The real index must match HEAD exactly, including intent-to-add entries. Any
  pre-existing staging blocks and remains byte-for-byte intact.
- Current changed paths must exactly match the candidate; extra files, removed
  changes and unrelated edits block. Every raw content hash and Git mode must match;
  replacing a candidate file with a deletion, or changing its bytes, blocks.
- Author/committer identity must resolve with `user.useConfigOnly=true`. Production
  does not invent identity or alter global/local configuration. Target/index Git
  environment overrides are not inherited. Tests use repository-local identity.

Acceptance owns the real index lock and stages in a temporary private index based
on base HEAD. Only the explicit candidate paths are inserted/removed using
`hash-object --no-filters` and NUL-delimited `update-index --index-info`; filenames
are not shell expressions or pathspecs. There is no `git add .` or `git add -A`.
The staged path set, modes and blob hashes are verified. Attribute/filter behavior
must also leave this tree clean against the worktree or acceptance blocks; raw
reviewed bytes are never silently transformed. Base, scope and fingerprints are
rechecked immediately before publication.

The deterministic commit message is `<source_flow>: accept reviewed V2 work`.
There is no model-written title. As in DELIVERY-001, `commit-tree` commits the
immutable verified tree, bypasses commit hooks, and `update-ref` uses old-HEAD
compare-and-swap. Required verification belongs before QA. Nothing pushes. A failed
CAS may leave an unreferenced commit object, but cannot overwrite competing history.
Before publication, the caller's real index is never changed; temporary staging is
discarded on failure. Worktree contents are never restored, reset or cleaned.
After publication, the prepared index replaces the old index atomically.

Post-publication checks require the expected HEAD, one direct descendant of base,
exact candidate paths/content/modes and clean tracked/nonignored worktree.
`ACCEPTED` is returned only after those checks. An unexpected invariant failure is
`COMMIT_BLOCKED` with `post_commit_verification_failed` and `committed_head`: this
is a serious **non-rollbackable** outcome, not a claim that HEAD stayed unchanged.
No reset, rebase, deletion of work or rewrite of the commit is attempted. Ambiguous
ref-update failures are checked against HEAD; unverifiable publication requires
manual recovery. External concurrent commits/edits are preserved, never undone.

Bounded diagnostics contain fixed reason codes, not file contents or raw Git stderr:
`base_head_changed`, `candidate_drift`, `unexpected_worktree_paths`,
`unexpected_staged_paths`, `git_conflict_state`, `git_identity_missing`,
`commit_failed`, `post_commit_verification_failed`, `worktree_identity_changed`,
`unsupported_path`, `empty_candidate` and `git_unavailable`. Invalid candidate
capture raises `LifecycleError` before publishing a candidate. Repository guards
use `git_conflict_state` for unsafe/unavailable Git inspection during acceptance.

### Runtime and CLI boundary

This is a small deterministic controller, not a second workflow engine: it has no
model outcomes, routing table, budgets or provider registry. The append-only
lifecycle artifacts are its audit record; source runtime run/correlation identities
are reused as provenance. It does not fabricate runtime Events for Git transaction
steps. Runtime/Event and delivery graphs remain unchanged. The generic CLI handles
V1 flow files and DELIVERY-001's CLI handles its pre-staged approval protocol; V2-007
therefore exposes only the reusable Python API.
