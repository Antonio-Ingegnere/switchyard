# Provider token usage (ECONOMICS-001, #2)

```
switchyard work usage WORK [--json] [--run R] [--state S] [--agent A] [--provider P] [--model M] [--invocation ID|I<n>]
switchyard.usage.read_usage(work_root, ...) / switchyard.usage.aggregate(records)
```

This records the token usage a provider reports for each invocation, keyed by the existing `invocation_id` and
correlated with `work trace`, `work diagnostics` and `work logs`. It is observational accounting: it never changes
execution, retries, budgets or a delivery's outcome. It includes no pricing, cost estimates or billing.

## Sources

| `source` | Meaning |
|---|---|
| `provider_reported` | Counts the provider reported for this invocation, normalized below. A failed invocation may still report usage. |
| `unavailable` | No trustworthy usage. The `reason` says why. |
| `not_applicable` | A command verifier, which consumes no model tokens. |
| `estimated` | Reserved. Never written: ECONOMICS-002 prompt estimates are separate records ([prompt composition](prompt-composition.md)) and never become usage. |

`unavailable` reasons:

- `not_reported`: the provider reported no usage.
- `no_result_envelope`: killed, timed out, or no final envelope was produced.
- `not_reported_in_exec_mode`: Codex before 2.6.1, which ran `codex exec` without `--json`.
- Codex since 2.6.1:
  - `no_jsonl_events`: no events arrived, for example when a CLI rejects `--json`.
  - `turn_failed_without_usage`.
  - `unverified_zero_usage`: an all-zero `turn.completed`, the CLI's default when no token update arrived.
  - `usage_capture_failed`: a parser fault.
- `not_launched`: the process never started.
- `invalid_provider_usage`: see `invalid_fields`.
- `not_captured`: recovery backfill.
- `not_recorded` / `not_recorded_by_this_runtime_version`: read-time only, never persisted.

Every count is a nullable integer. `null` means unknown, never zero. A zero appears only when the provider reported
zero. Counts must be non-negative integers no larger than 10¹². Booleans, floats, strings, negative or oversized
values make the whole usage `unavailable` / `invalid_provider_usage`; nothing is coerced.

## Provider semantics

| | Claude (`--output-format json`) | Codex (documented `exec --json` events) |
|---|---|---|
| Source | the ONE final result envelope's `usage` | `turn.completed.usage` from the `--json` stream |
| Basis | `final_cumulative_result`: all turns of the process, including StructuredOutput repair; taken once, never summed | `cumulative_thread_total`: the exec thread's running total; the latest snapshot wins, never summed; an earlier snapshot larger than the latest is invalid |
| input | `input_tokens`, **excluding** cache tokens | `input_tokens`, **including** `cached_input_tokens` |
| cache_read | `cache_read_input_tokens` | `cached_input_tokens` |
| cache_write | `cache_creation_input_tokens` | `cache_write_input_tokens`, only when reported (else `null`) |
| output | `output_tokens` | `output_tokens`, **including** reasoning; `reasoning_output_tokens` → `output_detail.reasoning_output` (a subset, never added) |
| Scope | top-level `usage` = **top-level agent loop only** (excludes subagent, sidechain and auxiliary model calls) | exec thread total; coverage of spawned agent threads not documented (`exec_thread_total_unverified`, so `partial`) |
| Model | `modelUsage` (whole agent tree, per model): one consistent model gives `reported_model`. Several give `multi_model_breakdown`, kept per model and never added to totals. A mismatch gives `inconsistent_breakdown`; an unusable one gives `unknown`. | not reported |
| Not recorded | `total_cost_usd` (billing contract unverified), session ids, payloads | — |
| Wired | 2.6.0 | 2.6.1 (`codex exec --json`) |

### Codex JSONL capture (2.6.1)

The adapter runs `codex exec --json`. All other flags are unchanged: sandbox, approvals, model, effort, capability
switches, `--output-schema` and `--output-last-message`. The StageResult comes ONLY from the `--output-last-message`
file, validated exactly as before. stdout is usage telemetry, and an `item.completed` agent message is never read as
a result.

The contract was verified against codex-cli 0.166.0 (codex-rs `exec`):

- Event types are `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.*` and `error`.
- `turn.completed.usage` holds `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens` and
  `reasoning_output_tokens`. It is built from the thread's last cumulative token total.
- When no token-usage update arrived, the CLI fills it with zeros by default.
- Exec runs one turn and shuts down on any terminal turn status.

`CodexUsageStream`, a `stdout_observer` of the shared process transport, works as follows:

- It splits lines across arbitrary chunk boundaries and buffers at most 64 KiB of one line.
- A longer line is skipped while the stream keeps draining. An oversized line that starts as a `turn.completed` makes
  the usage invalid (`oversized_turn_completed_event`).
- Malformed and unknown events are counted only, never logged.
- It keeps only the validated counters of up to 64 `turn.completed` snapshots.
- Any internal error detaches it, which yields `usage_capture_failed`. The process, its draining, the StageResult,
  retries and recovery are never affected.

The usage is noted even when `execute` raises (timeout, signal, I/O), so only what the stream reported counts.
Durable records receive counters only: never events, messages, reasoning, commands, tool output, file changes, thread
ids or prompts.

### Coverage scope (Claude)

The Agent SDK cost-tracking contract is
([Track cost and usage](https://code.claude.com/docs/en/agent-sdk/cost-tracking)):

- **`usage`:** "Counts only the top-level agent loop, so tokens consumed inside subagents are not added". It
  "undercounts as soon as nesting occurs".
- **`modelUsage`:** "Counts subagent requests alongside the top-level loop, broken down by model".

Each Claude record therefore carries a `coverage_scope`, and only one value proves full coverage:

| `coverage_scope` | Meaning |
|---|---|
| `invocation_wide_reconciled` | The per-model whole-tree breakdown sums, per category, exactly to `usage`. No subagent or auxiliary tokens exist beyond the top-level loop, so `usage` covers the invocation. |
| `top_level_agent_loop` | A breakdown exists and exceeds `usage`, which is evidence of excluded calls. This value is also used for `error_max_budget_usd`, whose `usage` omits the response that crossed the budget. |
| `top_level_agent_loop_unverified` | No breakdown was reported; the scope cannot be proven. |

A record is `complete` only when all four categories are known AND its scope is `invocation_wide_reconciled`.
Otherwise it is `partial`, with `completeness_reasons` (`categories_not_reported`, `scope_not_invocation_wide`).

The breakdown and its per-category sum (`model_breakdown_total`) are kept separately. They are never merged into
`tokens` or the totals, because the contract does not define how the two figures reconcile beyond equality.

`aggregate` reports these as well:

- `coverage_scope` counts and `invocation_wide` (true only when every reporting record is reconciled);
- a separate `agent_tree_by_model` sum.

`work usage` labels the totals "top-level agent loop as reported … may exclude subagent, sidechain and auxiliary
model calls; not complete invocation-wide consumption". When one exists, it prints the whole-tree per-model figure as
a separate block.

The contract also says an `error_during_execution` crash result "may carry zeroed" usage. An all-zero crash result is
therefore `unavailable` (`zeroed_crash_result`), never reported zeros.

`derived.input_including_cache` is computed per record from its own semantics. For Claude it is input + cache read +
cache write, and only when all three are known. For Codex it is the reported input, which already includes cached
input; it is `null` when a nonzero cache write is reported, because that write's relation to input is undocumented. Category totals are never added
across categories, because that would double-count cache tokens. When providers with different input semantics are
aggregated, `mixed_input_semantics` is set and the combined `input` sum is withheld (`withheld:
"mixed_input_semantics"`). Use the provider groups or the derived line instead. `cache_read` counts the same
quantity for both providers and stays summed. `output_detail.reasoning_output` is summed separately and never added
to output.

## Record and storage

Records live at `<work>/observability/usage/<invocation_id with ':' → '_'>.json`, with record schema 1. Each record
holds:

- `usage_id`: `sha256(work_id, invocation_id, "usage")[:32]`;
- work, run, visit and invocation ids;
- state, runtime state, rung and agent;
- provider, `configured_model`, `resolved_model`, `reported_model`, `model_allocation` and `model_breakdown`;
- source, reason and `invalid_fields`;
- `tokens {input, output, cache_read, cache_write}`, `semantics` (basis, provider fields, cache inclusion),
  `derived`, `output_detail` (`reasoning_output`, a subset of output; optional, absent before 2.6.1), `turns` and
  `duration_ms`;
- `completeness`: `complete` (all four categories known), `partial`, or `unavailable`;
- `observed_by`.

Records are immutable and published with an exclusive link. A retry, resume or repeated recovery never rewrites one.
They are bounded at 16 KiB and contain counts and code-owned metadata only: no prompts, responses, envelopes, streams,
environment, credentials or text.

## Lifecycle

- **Capture.** The adapter notes a pessimistic default before launch (`no_result_envelope` or `no_jsonl_events`).
  Only Claude's final envelope or Codex's observed stream replaces it.
- **Write.** The diagnostics writer's lifecycle writes the record (on invocation finish, on its interruption
  record, and on recovery backfill), independently failure-isolated. A failed write never changes the delivery,
  charges an attempt or triggers a retry.
- **Backfill.** A crash before the write leaves no durable usage evidence, because provider stdout is never persisted.
  The next drive therefore records `unavailable`/`not_captured` for invocations with a proven end. It never reruns a
  provider and never reconstructs counts.
- **Killed controller.** An invocation that only started gets no record. It reads as `unavailable` and is never zero.

## Aggregation and coverage

`aggregate` returns totals plus groups by run, state, agent, provider, model and invocation. Each invocation counts
once per grouping. Per group it gives:

- `invocations`, `provider_invocations`, `usage_reported`, `usage_partial`, `usage_unavailable` and `not_applicable`;
- per category, `{reported_sum, invocations_reporting}`, where `reported_sum` is `null` when nothing was reported;
- the derived input including cache.

The model group key is the attributed model: the reported single model, else the configured model when none was
reported, else `unallocated`. Totals are the KNOWN reported amount with explicit coverage, not a claim about actual
total consumption.

Reads never write, backfill or recover. A work without a usage store, from a runtime before 2.6.0, reads as
`not_recorded_by_this_runtime_version`. A 2.6.0 work whose writes failed reads as `not_recorded`.

## Out of scope

Billing reconciliation, token budgets and spend limits, routing by price, and debug-bundle export of usage. Estimated
list-price costs are a separate layer: see [economics](economics.md) (2.7.0).
