# Context Engine Design

## Principle

Context is explicitly assembled for an invocation.

Before the full Context Engine exists, every invocation must still receive an explicit immutable Work Unit payload. Provider session history must not implicitly become required context.

The runtime should not assume that a long-lived agent session is the authoritative project memory.

## Sources

Possible context sources:

- MicroSpec;
- repository `AGENTS.md`;
- selected architecture documents;
- selected ADRs;
- source files;
- previous normalized execution results;
- verifier failures.

## Selection

Each state should request only the context it needs.

Example:

```yaml
implement:
  context:
    - work_unit
    - repository_instructions
    - relevant_source_files

fix:
  context:
    - work_unit
    - previous_diff
    - failing_verification
```

## Durable versus ephemeral

Durable:

- project architecture;
- invariants;
- ADRs;
- accepted requirements.

Ephemeral:

- current failure output;
- current diff;
- temporary plan;
- provider session metadata.

Do not confuse them.

## Provider-native sessions

Adapters may use provider-native session/thread continuation for efficiency.

However:

- the runtime must be able to reconstruct required context without it;
- provider session state must not become the only copy of an important decision;
- session IDs are execution metadata, not architecture.

## External request sources (SOURCE-RESOLUTION-001)

V2 workspace context setup prepares supported external references before compiling
executors or starting the runtime. The pipeline is immutable request → source
resolution → durable source snapshot → entry-stage context → provider. In the
feature preset the entry stage is SPECIFY; bugfix starts at TRIAGE. All rungs of
that entry state receive the same snapshot. Later states use their declared
MicroSpec/artifact handoffs and do not repeat source material. Provider browsing
and provider-specific tools play no part in source resolution.

`source_resolution.SourceResolver` has two operations: `reference(candidate)`
returns a normalized `SourceReference` or None (recognition and canonical identity),
and `resolve(reference)` returns a `ResolvedSource`. The generic boundary detects
URLs in request text, keeps first-seen order and deduplicates by canonical URL.
`workspace_resolver(..., source_resolvers=(...))` supports explicit resolver/transport
injection; omission uses the built-in GitHub resolver. Unsupported URLs remain
ordinary text and are never fetched. Requests without supported references create
no source store and perform no network access.

The GitHub edge supports HTTPS `github.com/<owner>/<repo>/issues/<number>` and
`github.com/<owner>/<repo>/pull/<number>`, including URLs embedded in prose. Case,
trailing slash, query and fragment do not create distinct identities. It fetches
title, body, state, repository/number and top-level conversation comments (IDs and
bodies). PR inline review threads, checks and diffs are excluded. REST endpoints
are constructed locally; response links are never followed. Redirects are rejected.

The stdlib transport allows anonymous public access. It reads `GH_TOKEN`, falling
back to `GITHUB_TOKEN`, from the control-plane process environment, independently
of provider environments. A token is sent only in the GitHub API Authorization
header, never in URLs, artifacts, context or diagnostics. It is not saved. Exact
token echoes in fetched text are redacted before normalization. Failures use
code-owned explanations, never remote response bodies or transport exception text.

Bounds are constants at the resolver boundary:

| Resource | Limit / behavior |
| --- | --- |
| Unique supported references | 4; overflow fails before fetching |
| HTTP | 10-second socket timeout and elapsed deadline checked between reads; a final blocking read may take one additional socket timeout |
| HTTP response | 1 MiB, checked during streaming; overflow fails |
| Comments | Up to 2 pages × 10 comments per reference, API order; no retries |
| Title / body / comment body | 1024 / 8192 / 2048 UTF-8 bytes |
| State | 32 UTF-8 bytes |
| Aggregate source artifact | 24000 UTF-8 bytes of canonical JSON; overflow fails |
| Complete stage context | Existing 65536-byte limit, including source snapshots |

Field clipping retains complete UTF-8 characters and records each field's original
byte size and retained byte limit. Comment metadata records API-reported available
count, retained count and whether comments were omitted. The snapshot is a bounded
retrieval, not an atomic GitHub transaction: comments can change during pagination.
Aggregate overflow is explicit rather than silently dropping references. Narrow the
request or split references when that limit is exceeded.

`SourceResolutionError` extends the existing `WorkContractError` startup/input
failure boundary. Its diagnostic includes source type, canonical URL, classification
and an actionable explanation. Classifications distinguish authentication (401),
forbidden/rate limit (403), not found/access (404), timeout, network, malformed
response and limits. No SPECIFY invocation or runtime event is started on such a
failure. Recoverable delivery uses its existing `execution_interrupted` startup
exception handling: it reports INTERRUPTED, with no resumable provider invocation;
fix the input/access and start a fresh work. No human-input or terminal routing is
introduced. Source preparation does not consume semantic or infrastructure provider
invocation budgets.

See [durable source storage](work-contract.md#resolved-request-sources-source-resolution-001)
and [source transport](context-transport.md#resolved-source-transport).
