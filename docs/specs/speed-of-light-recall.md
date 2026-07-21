# Speed-of-Light Recall

**Status:** Implementing published-only lexical corpus
**Owner:** Longhouse session core
**Created:** 2026-07-20
**Depends on:** `agent-session-recall-continuity.md`,
`speed-of-light-database.md`

## Decision

Longhouse will make recall a low-latency, bounded service over the existing
disposable `search.db`. It will not add Elasticsearch, a vector database, a
remote search service, or another source of truth.

The target path is:

```text
agent / CLI
  -> long-lived MCP HTTP client
  -> /api/agents/recall
  -> one route-wide deadline
  -> bounded searchd admission
  -> small pool of independent SQLite read connections
  -> FTS5 top-K discovery
  -> bounded neighbor evidence from the same published search generation
  -> source-linked RecallResponse
```

Raw objects and catalog facts remain authoritative. `search.db`, ranks,
snippets, neighbor evidence, and term statistics are derived and rebuildable.

### July 20 correction: rank the eligible corpus

Production profiling disproved the remaining query-rewrite and global
candidate shortcuts. A broad query can match more than 140,000 historical FTS
rows, while only a small fraction survive the current-generation and requested
window filters. Ranking a fixed global top-K and filtering it afterward loses
valid results; deleting user terms changes their request.

The fast lexical lane therefore indexes only **published, current-generation
events inside a 91-day retained corpus, covering the normal 90-day discovery
horizon with a small clock margin,** into a separate `searchable_fts` corpus.
Search ranks that eligible corpus directly. Raw
`events` and its full FTS index remain the staging/archive source for explicit
wider (up to 365-day) requests, which are an honest slower lane rather than a
silent truncation. Successful publication atomically replaces one session's
searchable slice; supersession and deletion remove it.

Telemetry records search scope, token counts, result counts, admission delay,
and SQL time without raw query text. A schema-generation bump rebuilds this
disposable store cleanly; `VACUUM`, embeddings, and further worker scaling are
not the primary latency fix.

## Product Outcome

A user or agent can ask a natural question, paste an exact phrase, name a file
or flag, or provide a commit SHA and receive useful source-linked conversation
evidence in hundreds of milliseconds.

Five seconds is a cancellation fuse. It is not an acceptable response time.

## Measured Baseline

After the storage-v2 continuity cutover at commit `77bcbdedb`, the hosted
dogfood corpus measured:

| Observation | Result |
| --- | ---: |
| Corpus | 20,042 sessions; 28 GB `search.db` |
| Search WAL during active projection | 2.4 GB |
| Host | 8 vCPU EPYC Rome at about 2.45 GHz; 15 GB RAM |
| Direct network RTT from the current client | 28-38 ms |
| Reused HTTPS health request | 45-63 ms |
| Fresh HTTPS health request | 90-124 ms |
| Exact SHA searchd RPC | 3-5 ms |
| Exact SHA through MCP | 181-208 ms |
| Natural searchd RPC | 690-890 ms |
| Natural recall through MCP | about 1,002 ms p95 |
| Four simultaneous natural queries | 679, 1,351, 2,006, 2,671 ms |

The natural query used approximately one full CPU core with no meaningful disk
I/O. Searchd owns one read connection and one single-thread executor, so
concurrent calls form a serial queue while the other host cores remain idle.

`LonghouseAPIClient` also creates and closes an `httpx.AsyncClient` for every
tool call, paying TCP/TLS setup repeatedly.

Query shape is the other large cost. In the live corpus, `session` appears in
628,255 documents. For the same recovery question:

| Query form | Direct searchd latency |
| --- | ---: |
| Full natural sentence | 689-748 ms |
| Ordinary filler removed, `session` retained | 414-514 ms |
| Corpus-common `session` also removed | 60-72 ms |
| Exact phrase | 174-184 ms |

Finally, storage-v2 recall currently accepts `context_turns` but returns
`context=[]` and `total_events=0`. Fast snippets alone are not a complete recall
product.

## Diagnosis

The bottlenecks are:

1. avoidable per-tool network connection setup;
2. common-term FTS ranking and joins on one CPU core;
3. one search read worker and serial head-of-line blocking;
4. worklog sharing that same read worker;
5. mismatched 5-second searchd and 15-second HTTP/MCP deadlines;
6. missing bounded context despite the projected events already being present
   in `search.db`;
7. a large WAL with no explicit searchd checkpoint visibility.

The hardware is sufficient. The serving path does not use it well.

## Upstream Guidance

The implementation follows current upstream recommendations:

- keep `ORDER BY rank LIMIT n`; FTS5 documents it as faster than sorting on a
  direct `bm25()` call;
- use independent SQLite connections for concurrent WAL readers;
- keep WAL bounded because read cost increases as the WAL grows;
- use bounded `PRAGMA optimize` for planner statistics on long-lived
  connections;
- share one HTTPX `AsyncClient` on a hot path so its pool can reuse TCP/TLS
  connections.

This plan retains full FTS detail and stored column sizes. Reducing FTS detail
would save space but weaken exact phrase behavior, which recall requires.

## Goals

1. Typical discovery completes within 500 ms at MCP p95 on the largest hosted
   tenant under ordinary mixed load.
2. Exact identifiers complete within 175 ms at MCP p95 after connection
   warmup.
3. Four simultaneous typical searches complete without serial latency growth
   or errors.
4. Recall honors `context_turns` with bounded conversation evidence and
   canonical source locators.
5. Every slow request attributes time to admission, query compilation, SQL,
   evidence hydration, or transport.
6. WAL and planner degradation are observable and maintained outside request
   execution.
7. Self-hosting remains one Runtime Host plus SQLite and immutable local
   objects.

## Performance Contract

Discovery and evidence completion have separate budgets. Geography is also
separated from server processing.

| Surface | p95 | p99 | Hard boundary |
| --- | ---: | ---: | ---: |
| Searchd exact identifier RPC | 25 ms | 50 ms | 250 ms |
| Searchd typical natural RPC | 200 ms | 350 ms | 1 s |
| Warm MCP exact discovery | 175 ms | 250 ms | 1 s |
| MCP typical discovery | 500 ms | 750 ms | 2 s |
| MCP evidence-complete recall, `context_turns <= 2` | 750 ms | 1 s | 2 s |
| All-history lexical discovery | 1 s | 2 s | 5 s |
| Four-way typical discovery, slowest call | 750 ms | 1 s | 2 s |
| Recall error rate under mixed load | 0 target | below 0.1% | 0.1% |

The all-history target remains aligned with `speed-of-light-database.md`. Phase
0 must establish its real baseline before it becomes a release gate.

The API receives a five-second route budget. Searchd receives the remaining
budget minus a small response margin. The MCP transport deadline is long enough
to receive the API's typed timeout but does not restore the old 15-second wait.
Queue expiry prevents SQL from starting; in-flight expiry interrupts SQLite and
returns a typed retryable error.

## Architecture

### 1. Persistent MCP HTTP client

One `LonghouseAPIClient` owns one `httpx.AsyncClient` for the MCP server's
lifetime. All tools share it, and MCP shutdown calls `aclose()`.

Required behavior:

- shared base URL and auth headers;
- bounded keep-alive and connection counts;
- one request deadline supplied by the tool path;
- no retry that can exceed the deadline;
- lifecycle and transport-reuse tests.

HTTP/2 is optional and is not an acceptance criterion.

The same phase corrects MCP copy that still calls the canonical lexical route
“semantic/fuzzy” and removes retry guidance that points at retired stores.

### 2. Search query execution

Compact identifiers and explicit phrases retain their current deterministic
normalization.

The discovery index is a published-only recent corpus, not a second-stage
candidate list. Its FTS rows already satisfy current generation and the normal
window before ranking begins. A wider explicit request uses the archive lane,
whose slower behavior remains visible to the caller. Query compilation only
normalizes FTS syntax; it never removes user terms.

### 3. Searchd workload lanes

Searchd keeps the existing single write connection and adds:

- an interactive pool starting with two independent read-only connections;
- a separate single read-only connection for worklog snapshots.

Each connection has exactly one executor worker. Read connections open with
SQLite URI `mode=ro` and `PRAGMA query_only=1`; only the writer initializes or
changes schema.

The interactive pool may expand to four on an eight-core host only when the
four-way mixed-load benchmark proves improvement without harming catalogd,
projection, or ingest. Smaller self-hosted machines remain at one or two.

Searchd owns the authoritative bounded admission queue because more than one
client process may call it. `CatalogClient` retains only a generous safety cap;
it is not a second scheduling policy.

Every request records queue-entry, execution-start, and completion time. A
queued request that expires never executes SQL.

The current progress handler re-enters Python every 1,000 SQLite VM operations.
That can cause GIL contention when several FTS queries run concurrently.
Replace it with a deadline watchdog using the connection's thread-safe
`interrupt()` path, or prove with profiling that a much coarser progress
interval meets cancellation and parallelism targets. Cancellation cleanup is
tested independently on every pool connection.

Health's representative query uses normal interactive admission so saturation
is visible. Worklog cannot consume an interactive search worker.

### 4. Bounded evidence from the published search generation

Search discovery already returns `session_id`, `generation_id`,
`source_object_id`, `record_ordinal`, and event identity. `search.db.events`
already contains the clean projected event stream and has a session/generation
ordering index.

For each selected session, searchd performs one bounded neighbor query against
the same published generation as the hit:

1. locate the hit by its stable search identity;
2. walk ordered user/assistant conversation items around it;
3. return at most the requested zero to ten context turns;
4. apply a fixed total evidence-byte budget;
5. omit tool output by default;
6. include raw object and event locators for forensic drill-down;
7. obtain total event count from published session metadata rather than a
   request-time full count.

This avoids render-worker process fan-out and prevents a newer render
generation from being mixed with an older search hit. The evidence is visibly a
derived projection; raw objects remain canonical.

Each match reports `evidence_status=complete|partial|unavailable` and a typed
reason when incomplete. Snippets may still return when neighbor evidence misses
its sub-budget, but the response may not present them as complete context.

Full raw tool results and branch-forensic replay remain separate drill-down
operations. No LLM summarization runs in recall.

### 5. Boring maintenance

Maintenance is deliberately smaller than a new subsystem.

At startup:

- record database, WAL, and shared-memory sizes;
- run bounded `PRAGMA optimize=0x10002` on the writer;
- expose whether planner statistics exist;
- observe a passive checkpoint result without blocking readers.

After projection publication:

- run a passive checkpoint;
- record busy, log-page, and checkpointed-page counts;
- record WAL size;
- run ordinary `PRAGMA optimize` at most daily.

If the WAL remains large across multiple successful publications, first prove
the blocking reader or transaction. Only then add a bounded admission drain and
stronger checkpoint policy. Do not begin with `RESTART` or `TRUNCATE`.

Do not run `VACUUM`, full `ANALYZE`, forced checkpoints, or FTS5 `optimize` in a
request. FTS segment merge/optimize and SQLite cache/mmap tuning are deferred
until profiling shows they are needed.

### 6. Focused telemetry

Reuse `ServerTimingRecorder` and existing response-phase patterns. Record only:

- `admit`;
- `compile`;
- `sql`;
- `hydrate`;
- `total`;
- queue depth and active interactive readers;
- result/evidence counts;
- compiled token count, without raw query text;
- timeout/error phase;
- sampled database and WAL size.

Do not use raw query text, content, session IDs, or tokens as metric labels.

Search health reports representative-query latency, queue wait, projection lag,
WAL state, and failure reason independently.

## Implementation Plan

### Phase 0: Baseline, deadline truth, and result fixtures

- Add fixture queries for SHA, filename, flag, exact phrase, natural language,
  common terms, filters, and misses.
- Capture expected session identities and evidence locators so speed cannot hide
  relevance drift.
- Add serial, four-way concurrent, and mixed projection/read modes.
- Add `admit`, `compile`, `sql`, `hydrate`, and `total` timings.
- Align route, searchd, and MCP deadline behavior around the five-second fuse.
- Record warm-process, cold-process/warm-kernel, and separately authorized
  cold-host results without conflating them.

**Exit:** the benchmark is reproducible and a timeout reports the phase that
consumed the budget.

### Phase 1: Remove transport tax and product-copy drift

- Make `LonghouseAPIClient` persistent and closeable.
- Add connection-reuse and clean-shutdown tests.
- Correct lexical recall descriptions and retired fallback guidance.
- Measure the exact-query delta; do not require pooling alone to achieve the
  final 175 ms target.

**Exit:** warm calls reuse a connection and exact recall improves by the
measured TCP/TLS setup cost without leaks.

### Phase 2: Reduce per-query work

- Publish only current-generation events inside the 91-day retained discovery
  corpus into `searchable_events` / `searchable_fts`; the normal 90-day window
  uses this fast lane with a small clock margin.
- Rank that corpus directly; never globally rank then over-fetch/filter.
- Keep the full staging/archive index only for explicit wider search windows.
- Bump the disposable search schema generation and rebuild cleanly.
- Gate the cutover on session identities, filters, and phrase/identifier
  fixtures; never delete user terms as a latency policy.

**Exit:** representative recent natural searchd p95 is at or below 200 ms, or
the remaining measured cost is documented before changing the retrieval
architecture again.

### Phase 3: Return honest bounded evidence

- Add the generation-consistent neighbor query in searchd.
- Return source locators, total published event count, evidence status, and
  bounded context.
- Test revision changes, missing rows, byte limits, zero context, and evidence
  deadline exhaustion.

**Exit:** storage-v2 `context_turns` works and evidence-complete recall meets its
separate 750 ms p95 budget.

### Phase 4: Remove read head-of-line blocking

- Add two independent read-only interactive connections and one worklog
  connection.
- Make searchd admission authoritative.
- Replace or coarsen progress-handler cancellation based on profiling.
- Test queued expiry, in-flight interruption, worker recovery, concurrent
  projection, and worklog isolation.
- Expand from two to four readers only if hosted evidence supports it.

**Exit:** four simultaneous typical calls finish within 750 ms at p95 without
catalogd, ingest, projection, or worklog regression.

### Phase 5: Add bounded maintenance and production proof

- Add startup planner optimization and WAL/checkpoint observations.
- Add post-publish passive checkpoint and periodic bounded planner upkeep.
- Run a sustained projection/search/worklog soak.
- Run focused component tests per phase, then full backend and core E2E once at
  the final cutover SHA.
- Ship the exact SHA, verify build identity, and run serial/concurrent/mixed
  benchmarks on the hosted dogfood tenant.
- Compare controlled results with organic p95/p99 and error telemetry through a
  full work cycle.

**Exit:** the performance contract passes, WAL remains bounded or has a proven
blocker, and controlled and organic measurements agree.

## Validation Matrix

| Risk | Required proof |
| --- | --- |
| Connection leak | repeated MCP calls and clean server shutdown |
| Deadline mismatch | typed timeout from every layer within the route budget |
| Relevance regression | stable session identities across all query/filter fixtures |
| Unsafe candidate limit | identical filtered top-K or rejection of the optimization |
| Common-term CPU | corpus-common benchmark plus searchd CPU profile |
| GIL contention | two/four-reader CPU and wall-time profile |
| Queue poisoning | timeout followed by success on every reader |
| Worklog interference | worklog soak while interactive searches meet target |
| Reader/writer interference | maximum-safe projection while querying |
| WAL starvation | checkpoint counters and WAL size during sustained mixed load |
| Evidence dishonesty | explicit partial/unavailable reason and canonical locators |
| Cross-tenant leakage | owner-bound discovery and neighbor tests |
| Cold restart | reconstructable temporary vocabulary and healthy read pool |

## Rollout and Recovery

This is a pre-launch convergence. Do not retain parallel serving architectures.
Short-lived measurement gates are removed after acceptance.

Each change is independently reversible by a roll-forward commit:

- the MCP client can revert to per-call connections;
- a rejected SQL shape never replaces the proven query;
- natural compilation can be disabled without affecting identifiers/phrases;
- pool size can return to one;
- neighbor evidence can report typed partial status;
- passive maintenance can be disabled without affecting raw correctness.

No recovery path may delete raw objects, prune history, switch to a legacy
recall store, or present empty results as successful degradation. `search.db`
may be discarded and rebuilt because it is derived, but adding temporary term
statistics must not trigger a rebuild.

## Explicit Deferrals

- Elasticsearch, OpenSearch, Postgres, vector databases, or hosted search.
- Embeddings or LLM calls in default recall.
- Semantic claims for a lexical implementation.
- Render-object process fan-out for default neighbor context.
- A generic priority scheduler or unbounded connection pool.
- Hardcoded stopword lists as the primary optimization.
- FTS detail reduction that breaks phrase behavior.
- FTS segment merge/optimize without evidence.
- Cache-size or mmap tuning without profiling.
- Synchronous `VACUUM`, full `ANALYZE`, or forced checkpoint in requests.
- Precomputed prose summaries as authoritative memory.

## Open Measurements

1. Whether the candidate-first SQL form can preserve filtered top-K identity.
2. Whether two readers are sufficient after per-query optimization.
3. The document-frequency ceiling, if a compiler remains necessary.
4. The evidence byte budget that keeps `context_turns <= 2` below 750 ms.
5. The transaction or reader responsible if the WAL remains large after
   post-publish passive checkpoints.

These are benchmark choices, not reasons to introduce another architecture.

## Definition of Done

- MCP reuses connections and closes them cleanly.
- All recall layers honor one bounded deadline contract.
- Typical discovery meets 500 ms MCP p95 under serial and mixed load.
- Four simultaneous searches do not form a serial queue.
- Query optimizations preserve expected result identities.
- `context_turns` returns bounded generation-consistent evidence and locators.
- Worklog cannot occupy the interactive search lane.
- WAL and planner state are observable and maintained outside requests.
- Organic p95/p99 and error-rate telemetry exists.
- Five-second timeouts are exceptional, typed, cancellable, and below budget.
- No new external service or authoritative store exists.
