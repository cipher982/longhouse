# Agent Recall Retrieval Index

Status: implementation plan
Date: 2026-07-08

Longhouse recall should let an agent find prior work quickly without making the
Runtime Host scan or hydrate the whole session corpus. The primary consumer is a
coding agent, not a human search page. The API should return compact,
source-linked evidence fast and let the reading agent decide what it means.

## Success Criteria

- `/api/agents/recall` no longer needs to load the whole turn embedding corpus
  before answering a normal recall request.
- Default recall does not hydrate every durable event for matched sessions.
- Recall/search derived state lives in a dedicated `retrieval.db`, separate from
  the hot runtime/archive database.
- The first serving path is fast lexical recall over FTS5 child chunks with
  parent trace hydration.
- Tests cover schema initialization, chunk projection, lexical query behavior,
  parent hydration, filtering, and degraded fallback to legacy semantic recall
  where required.
- A labeled recall-quality fixture tracks recall@k/MRR for real agent queries so
  lexical V1 does not silently trade correctness for speed.
- A one-off profiling script can build a synthetic corpus and report p50/p95
  timings for projection, FTS query, hydration, and worst-case misses.
- The design leaves a clean path for vector embeddings and a USearch HNSW
  sidecar, but V1 does not depend on ANN to fix the current timeout.

## Problem

Current recall is coupled to raw durable state:

- it generates a query embedding before knowing whether the fast path can serve;
- it loads all session embeddings and all turn embeddings into an in-process
  numpy cache;
- it scans the full turn matrix and filters session metadata after scoring;
- it fetches all durable transcript events for matched sessions, then rebuilds
  clean transcript events to slice a small window;
- active-context recall adds more request-time boundary work.

On david010 the source database is large enough that this shape crosses request
timeouts on cold paths and makes every recall request fight the main SQLite
database.

## Decision: Dedicated `retrieval.db`

Use a separate SQLite database for recall serving state.

`retrieval.db` is a rebuildable cache, not source of truth. Raw sessions and
events remain in the main Longhouse database. This separation matters because
recall is large, read-heavy, and rebuildable; it should not bloat or lock the
hot DB paths used for ingest, runtime state, and control.

Implications:

- deleting `retrieval.db` never deletes user history;
- hosted tenants can place `retrieval.db` on faster local storage;
- FTS/vector maintenance cannot hold the hot DB write path hostage;
- backup policy can treat it as a cache with integrity checks;
- recall status must report missing, stale, or rebuilding index state plainly.

## First Implementation Scope

Build the lexical chunk index first.

In scope:

- retrieval DB path resolution from the archive database path;
- schema initialization for `recall_chunks`, `recall_chunks_fts`, and
  `recall_index_state`;
- deterministic projection from clean durable transcript events into parent and
  child chunks;
- index-on-demand/backfill helper for sessions touched by tests and local
  profiling;
- `/api/agents/recall` fast lexical mode when the retrieval index is available;
- parent hydration without raw event reads in the default response;
- diagnostics and status primitives enough to profile and debug.

The initial API default may use lexical retrieval only when the index is ready,
but that is a product decision with a quality gate, not just a performance
shortcut. Before replacing legacy semantic recall as the default for hosted
dogfood, run a small labeled query set and compare legacy semantic recall,
lexical chunk recall, and hybrid once embeddings land. If lexical misses common
conceptual queries, keep semantic/hybrid as the user-visible default while still
using lexical as a fast exact/code/path lane.

Out of scope for the first commit series:

- USearch dependency and HNSW sidecar;
- cross-encoder reranking;
- model-generated summaries or contextual retrieval strings;
- graph memory;
- background worker scheduling beyond simple idempotent projector entrypoints.

## Retrieval Store

`retrieval.db` contains only derived recall state.

### `recall_chunks`

One row per child evidence chunk or parent context chunk.

```sql
CREATE TABLE recall_chunks (
  id INTEGER PRIMARY KEY,
  chunk_uid TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  parent_session_id TEXT,
  thread_id TEXT,
  parent_thread_id TEXT,
  parent_chunk_id INTEGER,
  chunk_index INTEGER NOT NULL,
  chunk_kind TEXT NOT NULL,
  retrieval_role TEXT NOT NULL DEFAULT 'child'
    CHECK (retrieval_role IN ('child', 'parent')),

  event_index_start INTEGER NOT NULL,
  event_index_end INTEGER NOT NULL,
  first_event_id INTEGER,
  last_event_id INTEGER,

  provider TEXT,
  project TEXT,
  environment TEXT,
  device_id TEXT,
  cwd TEXT,
  git_repo TEXT,
  git_branch TEXT,
  started_at TEXT,
  last_activity_at TEXT,

  content TEXT NOT NULL,
  intent_text TEXT,
  evidence_text TEXT,
  structured_text TEXT,
  content_hash TEXT NOT NULL,
  token_count INTEGER NOT NULL DEFAULT 0,

  transcript_revision INTEGER NOT NULL DEFAULT 0,
  indexed_at TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0
);
```

Important indexes:

- `(session_id, chunk_index)`;
- `(parent_chunk_id)`;
- `(retrieval_role, started_at, id)`;
- `(project, started_at, id)`;
- `(provider, started_at, id)`;
- `(environment, started_at, id)`;
- `(content_hash)`.

### `recall_chunks_fts`

FTS5 over child evidence rows by default. The table is external-content, so the
projector must maintain it explicitly:

- delete old FTS rows before deleting/replacing projected chunks for a session;
- insert FTS rows for new child chunks only;
- provide a full `rebuild` path using `INSERT INTO recall_chunks_fts(recall_chunks_fts) VALUES('rebuild')`;
- provide an integrity check that compares child chunk counts with FTS row
  counts and records failures in `recall_index_state`.

```sql
CREATE VIRTUAL TABLE recall_chunks_fts USING fts5(
  content,
  intent_text,
  evidence_text,
  structured_text,
  cwd,
  git_repo,
  git_branch,
  content='recall_chunks',
  content_rowid='id',
  tokenize='unicode61 tokenchars ''._/-:''
);
```

Parent rows can live in `recall_chunks`, but the default FTS and embedding
serving path should only index child evidence rows. This keeps ranking sharp and
lets hydration fetch a larger parent trace after ranking.

Do not use Porter stemming for coding-agent recall. Agents search file paths,
snake_case symbols, flags, branch names, and exact error strings. Tokenizer tests
must cover:

- `server/zerg/routers/agents_search.py`;
- `--no-verify`;
- `source_lines`;
- `feature/recall-index`;
- `OperationalError`.

### `recall_index_state`

Small operational state table:

```sql
CREATE TABLE recall_index_state (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Required keys over time:

- `schema_version`;
- `projector_watermark`, based on max durable `AgentEvent.id` projected;
- `last_projection_error`;
- `last_integrity_check`;
- `vector_index` later.

## Chunking Model

Chunking is the highest-risk product choice. The V1 rule is parent-child
retrieval:

```text
child evidence chunk = retrieval and ranking unit
parent trace chunk = default context returned to the agent
raw events = optional deeper inspection through session detail
```

Child chunks target 200-500 tokens. Parent trace chunks target 800-1,800 tokens
with a hard 2,500 token cap. Split on transcript structure first, not arbitrary
token windows.

Chunk kinds:

| Kind | Role | Purpose |
| --- | --- | --- |
| `trace_parent` | parent | one user intent plus following assistant/tool run |
| `turn_pair_parent` | parent | compact user plus assistant pair for simple sessions |
| `intent` | child | user prompt, redirect, or explicit task wording |
| `assistant_conclusion` | child | final answer, decision, or next-step synthesis |
| `tool_result` | child | capped command/tool output with unique searchable evidence |

V1 should stop there. `structured_fact` and `session_card` are useful later, but
they add tuning surface before the core projector is proven. For V1, write
deterministic file/cmd/tool/branch/error tokens into `structured_text` on the
child chunks above.

Avoid indexing full tool output. Prefer tool name, command/input, file path,
first useful error/status line, capped output excerpt, and event ids.

## Query Flow

Inputs:

```text
query
project?
provider?
environment?
since_days?
max_results
context_turns
mode = lexical | semantic | hybrid
```

V1 default behavior:

1. Try lexical retrieval from `retrieval.db` if the index is initialized and has
   child chunks.
2. Normalize user text into a safe FTS5 query and structured prefix filters.
3. Query child rows only with metadata filters.
4. Over-fetch at least `max(max_results * 20, 100)` rows so chatty sessions do
   not starve diversified results.
5. Diversify to one result per parent session by default.
6. Hydrate selected child rows and their parent rows in one batch.
7. Return compact match evidence, parent context, stable ids, and diagnostics.
8. If the retrieval index is unavailable and the caller requested semantic
   legacy behavior, use the old path with explicit degraded diagnostics.

Example lexical leg:

```sql
SELECT c.id, bm25(recall_chunks_fts) AS score
FROM recall_chunks_fts f
JOIN recall_chunks c ON c.id = f.rowid
WHERE recall_chunks_fts MATCH :fts_query
  AND c.retrieval_role = 'child'
  AND (:project IS NULL OR c.project = :project)
  AND (:provider IS NULL OR c.provider = :provider)
  AND (:environment IS NULL OR c.environment = :environment)
  AND (:since IS NULL OR c.started_at >= :since)
ORDER BY score
LIMIT :inner_limit;
```

`bm25()` returns lower scores for better matches. Keep `ORDER BY score` ascending
and test that ranking direction explicitly.

## API Shape

Keep the existing `RecallResponse` compatible. Add optional fields to
`RecallMatch` rather than breaking clients:

- `chunk_id`;
- `chunk_uid`;
- `parent_chunk_id`;
- `context_chunk_id`;
- `chunk_kind`;
- `context_text`;
- `intent`;
- `evidence`;
- `structured_hits`;
- `diagnostics`.

The endpoint should keep returning the existing `context` list for old clients,
but the fast path should build it from indexed child/parent rows rather than raw
event hydration.

Existing compatibility fields need fast-path definitions:

- `total_events`: persist the parent trace clean-event count on the parent row,
  or return the parent window count for indexed matches. Do not load every event
  just to populate this field.
- `match_event_id`: use `first_event_id` from the child row.
- `event_index_start` / `event_index_end`: use the indexed clean-event bounds.
- `context`: synthesize from parent/child content and indexed bounds. It is a
  compact compatibility projection, not proof that the entire session was read.

The fast path must check retrieval-index availability before importing or
constructing `EmbeddingCache`.

## Freshness

V1 freshness is based on real durable event ids, not an abstract transcript
revision:

- each projected session records the max durable `AgentEvent.id` and durable
  event count covered by its chunks;
- the global `projector_watermark` records the highest durable event id fully
  projected;
- active sessions may be slightly stale, but `recall_status` and response
  diagnostics must report staleness;
- on-demand projection by session id is allowed when a recall query would
  otherwise hit a stale or missing session, but it must use bounded work and
  never block live ingest/control.

## Profiling Plan

Add a one-off script under `scripts/dev/` that can:

- create a synthetic main DB with configurable sessions, events, projects, and
  providers;
- project it into a temporary `retrieval.db`;
- run representative hit, miss, filtered, and worst-case broad queries;
- report p50, p95, max, row counts, and DB file sizes;
- compare legacy recall cost with the matrix load preserved and only the network
  query-embedding call mocked;
- measure archive DB write latency during concurrent recall load to prove the
  `retrieval.db` isolation win, not just recall latency.

Initial profiles:

- 1k sessions / 20 events;
- 10k sessions / 20 events;
- one giant session with thousands of events;
- high-duplicate command output;
- filtered project/provider queries;
- miss query with no FTS hits.
- read-only copy of a real large hosted DB when available.

## Test Plan

Use `make test` targets, with focused tests in `server/tests_lite/`.

Planned coverage:

- retrieval DB initializes without touching main DB schema;
- FTS5 table is present and child rows are searchable;
- parent rows are not returned as primary hits;
- projection creates parent/child chunks from clean transcript events;
- tool output is capped and structured tokens are extracted;
- FTS maintenance deletes old rows before reprojecting a session;
- FTS tokenizer preserves code paths, flags, snake_case, branch names, and error
  names;
- BM25 ascending order ranks the better match first;
- project/provider/environment/since filters apply before result hydration;
- recall fast path avoids `EmbeddingCache.load_turn_embeddings`;
- default hydration fetches parent context without querying all session events;
- legacy semantic path still works when explicitly requested or when retrieval
  mode is unavailable.

## Commit Plan

1. Spec and plan.
2. Retrieval DB schema/path/init service plus FTS maintenance tests.
3. Chunk projector plus tests.
4. Lexical query service plus tests.
5. Recall endpoint integration plus compatibility tests.
6. Profiling script and recorded local timing notes.
7. Review cleanup after hatch DeepSeek/Opus feedback.

## Later Vector Path

After lexical recall proves the DB isolation win:

- add `recall_embeddings` for child chunks only;
- add query embedding cache;
- build a USearch HNSW sidecar keyed by `recall_chunks.id`;
- fuse FTS and vector results with reciprocal rank fusion;
- gate rerankers/contextual retrieval behind eval results.

The important invariant stays the same: vectors and FTS point to the same child
chunk ids, and parent trace hydration happens after ranking.
