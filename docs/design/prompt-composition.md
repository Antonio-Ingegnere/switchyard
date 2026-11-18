# Prompt composition estimation (ECONOMICS-002, #2)

```
switchyard work prompt-estimate WORK [--json] [--summary] [--run R] [--state S] [--agent A] [--provider P] [--model M] [--invocation ID|I<n>]
switchyard.prompt_estimate.report(work_root, ...) / read_estimates(work_root, ...)
```

`work prompt-estimate` shows what the **initial prompt that Switchyard itself supplies** to each provider invocation
is made of, and how large it is. The goal is to see which parts of Switchyard's deterministic context take the most
input space.

It covers client-supplied prompt composition only. It is not the provider's total context, total billed input tokens
or actual token usage. Actual usage stays with [`work usage`](usage.md) and is never replaced, added to or compared
with these estimates. Nothing here is priced.

## One render, measured

`provider_stages.render_prompt(invocation)` is the only prompt renderer. It builds the prompt as ordered sections and
returns three things:

- the text;
- the exact UTF-8 `data` the adapter writes to the provider's stdin;
- the composition of exactly those bytes.

`render_invocation()` returns the same text, byte-identical to 2.9.0. A test compares it with a frozen copy of the
2.9.0 renderer for representative invocations, and with what the fake Claude and Codex CLIs received on stdin.

Nothing is rendered twice. The measurement runs after the text exists, inside its own `try`. If it fails, the
composition is absent and the prompt, the arguments and the result are unchanged.

## Categories

Every transmitted byte belongs to exactly one category:

```
sum(component utf8_bytes) == len(transmitted prompt bytes)
```

The same holds for characters. A measurement that does not reconcile is discarded rather than recorded.

| Category | Bytes counted |
|---|---|
| `runtime_instructions` | Contract header, stage instruction, result rules paragraph |
| `execution_binding` | The binding JSON |
| `request` | Content of the runtime's `request` input: name `request` with no producer |
| `microspec` | Content of artifacts whose record schema is `microspec` |
| `durable_artifacts_other` | Content of every other durable input artifact |
| `human_clarification` | Content of the clarification records |
| `qa_feedback` | The findings list, rejection record contents, finding descriptions in the evidence section |
| `qa_evidence_text` | The shown text of text evidence |
| `qa_evidence_markers` | The rest of the evidence section: labels, hashes, image-input markers |
| `resolved_sources` | Content of the resolved request sources |
| `previous_handoff` | The handoff JSON |
| `verifier_diagnostics` | The diagnostic text |
| `execution_capabilities` | The capabilities section |
| `output_contract` | Stage output contract, MicroSpec and handoff guidance, result schema, structured-output field map |
| `serialization_overhead` | Section headings, JSON wrapping, artifact metadata (name, version, format, schema, records), quotes and the blank-line separators |

Rules for the counting:

- **Classification uses only artifact record metadata, never content.** A note that mentions "MicroSpec" is
  `durable_artifacts_other`, and so is a stage-produced artifact that happens to be named `request`.
- **Content is counted as transmitted, after JSON escaping.** An escaped quote or newline is two bytes of that
  content's category.
- **Absent categories are omitted.** A category that is not listed contributed no bytes.
- **`items` counts included records** for artifact-like categories. It is `null` elsewhere.

## Token estimate

The method is `heuristic_chars_per_token_v1`:

- the total is `ceil(characters / 4)`;
- that total is split across components by characters with a deterministic largest-remainder rule, so the component
  estimates always sum to the total.

It is an approximation and is not provider-tokenizer accurate, especially for code, non-English text and JSON. It is
never usage, never priced and never subtracted from provider-reported input to produce an "error". The two cover
different contexts.

No tokenizer dependency or model asset is used.

## Transport boundaries

Measured: the stdin payload of `claude --print …` and `codex exec … -`.

Recorded as separate transport metadata, never added to the prompt total:

- `native_output_schema_bytes`: the exact UTF-8 size of the generated StageResult schema. Claude receives it as a CLI
  argument (`cli_argument`), Codex as a file (`schema_file`). These are bytes, not billed tokens.
- `image_attachment_count` and `image_attachment_total_bytes`: Codex image evidence files. These are file bytes, never
  converted to text tokens. No content, path or name is stored. Claude has no image transport and refuses image
  evidence before rendering.

Unobserved, and listed in each record:

- **Both providers:** provider-internal context and system instructions, tool-generated context (including
  `repository_tool_context`), provider-generated messages, provider prompt caching.
- **Claude:** structured-output repair turns and subagent context.
- **Codex:** shell command output and additional agent threads.

Repository context is never estimated from disk size, file counts, diffs or shell output. The report says
`repository_tool_context: unobserved`.

## Record (schema 1)

The record is stored at `<work>/observability/prompt-estimates/<invocation id, ':' -> '_'>.json`. It is written
next to the usage record by the diagnostics writer, after the authoritative records. It is immutable, published by
an exclusive link, and at most 16 KiB.

Keys:

- `schema_version`, `record: prompt_composition`, `estimate_id`;
- `work_id`, `run_id`, `state_visit_id`, `invocation_id`;
- `state`, `runtime_state`, `rung`, `agent`, `provider`, `configured_model`, `resolved_model`;
- `capture_status`, `reason`, `scope: switchyard_supplied_initial_prompt`;
- `exact: {utf8_bytes, characters, sections}`;
- `components: [{category, utf8_bytes, characters, estimated_tokens, items}]`;
- `estimate: {method, characters_per_token, tokens, provider_tokenizer: false, basis}`;
- `transport: {prompt_channel, native_output_schema_bytes, native_output_schema_channel, image_attachment_count,
  image_attachment_total_bytes}`;
- `unobserved`, `observed_by`.

Only numbers, code-defined labels and runtime identifiers are stored. There is never prompt text, request,
MicroSpec, handoff, QA, source, evidence or diagnostic content, artifact names or paths, commands, environment values
or session identifiers.

## Capture

| `capture_status` | When |
|---|---|
| `submitted_or_launched` | The prompt was prepared and the process launched with it on stdin. Launch does not prove the provider consumed all of it, and nothing proves it was billed. Kept on provider failures after launch. |
| `prepared_but_not_launched` | Prepared, but the process never started. |
| `capture_unavailable` | `prompt_not_prepared`: failed before rendering, so there is no zero-length prompt. `not_captured`: recovery backfill after a controller crash. |
| `not_applicable` | Command verifiers, which use no model prompt. |

If the breakdown itself failed, the record keeps its launch status and exact totals, sets reason
`composition_measurement_failed`, and has no components and no token estimate.

After a crash the measurement is gone with the controller. Recovery backfills a `not_captured` marker for every
invocation with a proven end, the same rule as usage. It never re-renders from today's artifacts, reruns a provider
or changes an outcome.

A publication failure is swallowed. Execution, StageResult, provider arguments, retries, delivery state, QA and
recovery are unaffected, and so are the other records.

## Report (schema 1)

The report reads only the immutable records and the canonical invocation order. It never renders prompts, reads
artifacts, recovers, backfills, launches processes, runs Git, loads pricing or writes.

An invocation without a record reads as `capture_unavailable` with one of two reasons:

- `historical_prompt_composition_not_captured`: the work has no store and was created before 2.10.0;
- `not_recorded`: otherwise.

The report keys are:

- `schema_version`, `report: switchyard_prompt_composition`, `work_id`, `composition_recorded`, `scope`, `estimation`;
- `invocations`, with `change_from_previous_same_state` (an exact byte delta);
- `summary`;
- `coverage_flags`;
- `actual_usage: separately_reported_by_work_usage`, `estimated_cost: null`, `repository_tool_context: unobserved`.

`summary` aggregates **repeated submissions, not unique information**. A MicroSpec sent in two invocations is counted
twice; that is not called avoidable. Its fields are:

- `capture_status` counts, `measured`;
- `utf8_bytes`, `characters`, `estimated_tokens`, each `null` when nothing was measured, never zero;
- `by_category`, with an invocation count per category;
- `largest_components`, ranked by bytes;
- the separate schema and image transport sums.
