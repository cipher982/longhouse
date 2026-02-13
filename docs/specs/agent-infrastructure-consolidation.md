# Agent Infrastructure Consolidation: Life Hub → Longhouse

**Date:** 2026-02-12
**Status:** v3 — Final (Codex-reviewed, issues addressed)
**Goal:** Make Longhouse the canonical home for all agent infrastructure, then deprecate Life Hub's agents schema.

---

## Context

Longhouse and Life Hub both store agent session data independently. They diverged:

| Metric | Life Hub (Postgres) | Longhouse (SQLite) |
|--------|--------------------|--------------------|
| Sessions | 4,947 | 10,446 |
| Events | 1,619,570 | 736,991 |
| Earliest | 2025-07-16 | 2025-08-10 |
| Providers | claude, codex, gemini, cursor, swarmlet | claude, codex, gemini |
| Summaries | 0 | 10,442 |
| Embeddings | 7,022 sessions (pgvector, 1536-dim) | 0 |
| FTS search | None | FTS5 on events |

Longhouse has 2x the sessions but zero embeddings. Life Hub has embeddings, insights DB, and file reservations — used daily via MCP in every Claude Code session.

### Raw Data Audit

| Provider | raw_json stored? | Code ref |
|----------|-----------------|----------|
| Claude | Yes | `parser.py:303,307` — original JSONL line verbatim |
| Codex | Yes | `codex.py:154,193,214,231` — original JSONL line |
| Gemini | **No** | `gemini.py:182,210,231` — `raw_line=""` hardcoded |

Gemini = 3.3% of sessions (346/10,446). Fix is trivial (store the JSON message object as raw_line). Existing Gemini sessions have parsed fields intact — no data loss, just no verbatim original.

---

## What Migrates, What Stays, What Gets Cut

### Migrate to Longhouse (agent infrastructure)

| Feature | Life Hub impl | Value | Complexity |
|---------|--------------|-------|------------|
| **Session embeddings** | pgvector, 1536-dim, HNSW | Core differentiator — "find where I solved it" | Medium |
| **Semantic search** | `search_agent_logs` MCP tool | Daily use, better than ILIKE | Low (piggybacks on embeddings) |
| **Recall** | `recall` MCP tool, chunk-level | "you solved a similar problem 2 weeks ago" | Medium |
| **Insights DB** | `work.insights` table, `log_insight`/`query_insights` | Every session checks before starting work | Low (one table, two endpoints) |
| **File reservations** | `work.reservations`, reserve/check/release | Critical for swarm mode (concurrent agents) | Low (one table, three endpoints) |

### Stays in Life Hub (not agent infra)

| Feature | Why |
|---------|-----|
| Smart home (Zigbee2MQTT) | Personal IoT, not agent infrastructure |
| Gmail (full API) | Longhouse already has send-only for digest |
| Google Drive | Personal file management |

### Cut (not worth porting)

| Feature | Life Hub impl | Why cut |
|---------|--------------|---------|
| **`query_agents` raw SQL** | Exposes raw SQL over agents schema | Security risk in SQLite (`ATTACH` can read arbitrary files). Longhouse already has FTS5 + semantic search + structured API. Raw SQL is a power-user escape hatch — replace with structured query endpoints. |
| **Session scoring (kNN)** | `session_scores` + `session_labels` tables | David-specific quality scoring against manually-labeled anchors. No OSS user would use this. Adds schema (2 tables), labeling workflow, and scoring pipeline for marginal value. |
| **Task dependencies** | `blockedBy`/`blocks` + `work.prioritized` view | Life Hub uses a Postgres materialized view with leverage_score — not portable to SQLite. Longhouse already has basic tasks (create/list/update/delete). Task deps add JSON-array querying pain in SQLite for limited value. If needed later, it's a small addition — don't build it speculatively. |
| **Task MCP tools** | `create_task`/`ready_tasks`/`update_task`/`close_task` | Longhouse already has equivalent Oikos tools (`task_create`/`task_list`/`task_update`/`task_delete` in `tools/builtin/task_tools.py`). No need to duplicate in MCP. |

---

## Embedding Design

### Model: 256 Dimensions

Both OpenAI and Gemini use Matryoshka training — first N dimensions carry the most information. Truncation preserves quality far better than linear:

**Gemini `gemini-embedding-001` MTEB by dimension:**

| Dims | MTEB | Delta from max | Bytes/vector |
|------|------|----------------|--------------|
| 3072 | 68.2 | baseline | 12,288 |
| 1536 | 68.17 | -0.04% | 6,144 |
| 768 | 67.99 | -0.3% | 3,072 |
| 512 | 67.55 | -0.95% | 2,048 |
| **256** | **66.19** | **-2.9%** | **1,024** |
| 128 | 63.31 | -7.2% | 512 |

**OpenAI uses 256-dim `text-embedding-3-large` as the default in their own `file_search` production tool.** If it's good enough for OpenAI's retrieval product, it's good enough for session search.

For "find where I fixed the auth bug" queries, 97% quality at 1/12th the storage is the right tradeoff.

### Scale at 256 Dims

Embedding is a **compression** of text, not an expansion:

| Content | Text size | Embedding size | Ratio |
|---------|-----------|---------------|-------|
| Short user message | ~500 bytes | 1,024 bytes | 2x expansion |
| Typical assistant turn | ~5 KB | 1,024 bytes | 5x compression |
| Tool output (file contents) | ~50 KB | 1,024 bytes | 50x compression |
| Large grep/read result | ~200 KB | 1,024 bytes | 200x compression |

**Session-level search (one embedding per session):**
- 10K sessions × 1KB = **10 MB** — trivially in memory, ~5ms search

**Turn-level recall (one embedding per meaningful turn):**
- ~350K turns × 1KB = **~340 MB** in memory
- numpy brute-force at 256 dims: **~15ms** for 350K dot products
- Compare: the events text table is already multi-GB in SQLite

340MB is a rounding error relative to the text data it represents. No two-stage optimization needed at this scale. (If someone hits 100K+ sessions, ~3.5M turns at 3.4GB, revisit then — but single-tenant will likely never get there.)

### Provider Configuration

```python
# config/models.json (extend existing model config pattern)
"embedding": {
    "default": {
        "provider": "gemini",
        "model": "gemini-embedding-001",
        "dims": 256,
        "apiKeyEnvVar": "GEMINI_API_KEY"
    },
    "alternatives": {
        "openai": {
            "model": "text-embedding-3-small",
            "dims": 256,
            "apiKeyEnvVar": "OPENAI_API_KEY"
        }
    }
}
```

Follows existing `models.json` pattern for LLM config. OSS users set their preferred provider.

### Storage Model

```python
class SessionEmbedding(AgentsBase):
    __tablename__ = "session_embeddings"

    id = Column(Integer, primary_key=True)
    session_id = Column(GUID(), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)

    # Embedding classification
    kind = Column(String(20), nullable=False)    # 'session' or 'turn'
    chunk_index = Column(Integer, default=-1)     # -1 = session-level, >=0 = turn index

    # Event mapping (for recall context window retrieval)
    event_index_start = Column(Integer, nullable=True)   # first event index in chunk
    event_index_end = Column(Integer, nullable=True)      # last event index in chunk

    # Model tracking (for re-embedding if model changes)
    model = Column(String(128), nullable=False)   # e.g. 'gemini-embedding-001'
    dims = Column(Integer, nullable=False)         # e.g. 256

    # The vector (numpy float32 serialized to bytes)
    embedding = Column(LargeBinary, nullable=False)

    # Dedup / versioning
    content_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "kind", "chunk_index", "model", name="uq_session_emb"),
        Index("ix_session_emb_session", "session_id"),
        Index("ix_session_emb_kind", "kind", "chunk_index"),
    )
```

**Naming clarification** (from Codex review): Two kinds only, named clearly:
- `kind='session'`, `chunk_index=-1`: One per session. Used by `search_sessions`. Built from summary text or reduced turn embeddings.
- `kind='turn'`, `chunk_index=N`: One per meaningful turn. Used by `recall`. Built from user+assistant message pairs.

**Search always filters by `kind` + `dims`** to prevent cross-model nonsense scores (Codex review finding #6).

**Upsert strategy:** `ON CONFLICT (session_id, kind, chunk_index, model) DO UPDATE SET embedding, content_hash, created_at`. Re-embedding replaces in-place — no delete-then-insert needed.

### Chunking Pipeline

Reuse existing `session_processing` module. New file: `session_processing/embeddings.py`.

1. **Session-level embedding** (for search):
   - Input: `summary_title + ". " + summary` (already pre-computed for 10,442 sessions)
   - If no summary: build transcript via `build_transcript()`, truncate to 1800 tokens
   - One embedding per session, `kind='session'`, `chunk_index=-1`

2. **Turn-level embeddings** (for recall):
   - Build transcript, detect turn boundaries (user↔assistant role changes)
   - Each turn = concatenation of user message + assistant response
   - Truncate each turn to 1800 tokens (Gemini limit is 2048, 250-token buffer)
   - One embedding per turn, `kind='turn'`, `chunk_index=turn_number`

Token counting: use `tiktoken` for OpenAI or character-based estimate for Gemini (4 chars ≈ 1 token, with 20% safety margin). Both are conservative enough to avoid truncation at the API.

### Ingest Integration

**On ingest (background chain):**
```
POST /api/agents/ingest
  → store events (sync)
  → generate summary (BackgroundTask)
      → on summary complete: generate embeddings (BackgroundTask)
```

**Durability** (from Codex review finding #3): Add `needs_embedding` boolean column to `AgentSession` (default True). Embedding task sets it to False on success. Backfill endpoint queries `WHERE needs_embedding = True`. This catches silent failures without a separate job queue.

**Backfill endpoint:**
```
POST /api/agents/backfill-embeddings?batch_size=50&max_batches=10
```
Same pattern as existing `backfill-summaries`. Bounded batches, rate-limited.

---

## Work Schema: Insights + File Reservations

### Insights Table

```python
class Insight(AgentsBase):
    __tablename__ = "insights"

    id = Column(GUID(), primary_key=True, default=uuid4)
    insight_type = Column(String(20), nullable=False)  # pattern, failure, improvement, learning
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    project = Column(String(255), nullable=True, index=True)
    severity = Column(String(20), default="info")      # info, warning, critical
    confidence = Column(Float, nullable=True)           # 0.0-1.0
    tags = Column(JSON, nullable=True)
    observations = Column(JSON, nullable=True)          # Append-only list of sightings
    session_id = Column(GUID(), nullable=True)          # Source session (optional)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```

**Dedup behavior** (matching Life Hub exactly): Same `title + project` within 7 days → update `confidence`, append to `observations` list. This prevents duplicate "Coolify needs UFW rule" insights from accumulating.

**API endpoints:**
- `POST /api/insights` — create or deduplicate
- `GET /api/insights?project=zerg&since_hours=168` — query

**MCP tools:** `log_insight`, `query_insights` — same signatures as Life Hub's (`mcp_server.py:934-1133`).

### File Reservations Table

```python
class FileReservation(AgentsBase):
    __tablename__ = "file_reservations"

    id = Column(GUID(), primary_key=True, default=uuid4)
    file_path = Column(Text, nullable=False)
    project = Column(String(255), nullable=False, server_default="")  # non-null, empty = global
    agent = Column(String(255), nullable=False, default="claude")
    reason = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    released_at = Column(DateTime, nullable=True)       # NULL = active
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        # Only one active reservation per file+project
        Index("ix_reservation_active", "file_path", "project",
              unique=True,
              sqlite_where=text("released_at IS NULL")),
    )
```

**Expiry cleanup** (from Codex review finding #7): `reserve_file` cleans up expired reservations before inserting (same as Life Hub's `mcp_server.py:789-794`). No separate cleanup job needed — cleanup is opportunistic on write.

**API endpoints:**
- `POST /api/reservations` — reserve
- `GET /api/reservations/check?file_path=...&project=...` — check
- `DELETE /api/reservations/{id}` — release

**MCP tools:** `reserve_file`, `check_reservation`, `release_reservation` — same signatures as Life Hub's.

---

## MCP Server Expansion

### Current Longhouse MCP tools (5)

From `zerg/mcp_server/server.py`:
1. `search_sessions` — keyword search
2. `get_session_detail` — event log retrieval
3. `memory_read` — local JSON KV
4. `memory_write` — local JSON KV
5. `notify_oikos` — WebSocket notification

### New tools to add (5)

| Tool | Source | Notes |
|------|--------|-------|
| `search_sessions` (upgrade) | Phase 1 | Add `semantic=true` mode to existing tool |
| `recall` | Phase 1 | Chunk-level semantic search, returns conversation windows |
| `log_insight` | Phase 2 | Port from Life Hub verbatim |
| `query_insights` | Phase 2 | Port from Life Hub verbatim |
| `reserve_file` | Phase 2 | Port from Life Hub verbatim |
| `check_reservation` | Phase 2 | Port from Life Hub verbatim |
| `release_reservation` | Phase 2 | Port from Life Hub verbatim |

**Not porting** (with reasoning):
- `query_agents` raw SQL — security risk, replaced by semantic search + structured endpoints
- `create_task` / `ready_tasks` / `update_task` / `close_task` — Longhouse already has these as Oikos builtin tools

### After expansion: 10 MCP tools

1. `search_sessions` (semantic + keyword)
2. `get_session_detail`
3. `recall`
4. `memory_read`
5. `memory_write`
6. `notify_oikos`
7. `log_insight`
8. `query_insights`
9. `reserve_file`
10. `check_reservation` + `release_reservation`

---

## Historical Data Migration

### Backfill Missing Sessions (~270)

Life Hub has sessions Longhouse doesn't:
- 230 cursor sessions (Longhouse has no cursor data)
- ~41 pre-Aug-10 sessions (codex + early cursor)

**Approach:** Script that queries Life Hub Postgres directly (not MCP — need bulk access):
1. `SELECT id, provider, project, ... FROM agents.sessions WHERE provider = 'cursor' OR started_at < '2025-08-10'`
2. For each session, fetch events: `SELECT * FROM agents.events WHERE session_id = ?`
3. Map Life Hub fields → Longhouse `EventIngest` format:
   - `raw_text` → `raw_json` (lossless preservation)
   - `content_text`, `tool_name`, etc. map directly (same column names)
4. POST to `http://localhost:47300/api/agents/ingest` (or direct DB insert)

### Backfill Embeddings (all ~10,700 sessions)

After embedding pipeline is built:
1. Run `POST /api/agents/backfill-embeddings` in bounded batches
2. Both session-level and turn-level embeddings
3. Rate limit to stay within Gemini free tier quotas

### Migrate Insights History

Query existing insights from Life Hub and insert into Longhouse:
```sql
SELECT * FROM work.insights ORDER BY created_at
```
Direct DB insert into Longhouse's `insights` table. One-time script.

### Migrate Active Reservations

Not needed — reservations are ephemeral (60-minute TTL). By the time we cut over, all current reservations will have expired.

### Fix Gemini Raw Data

Update `gemini.py` parser to store the JSON message object as `raw_line` instead of `""`. Three lines of code. Existing sessions keep their parsed fields — the Gemini source files still exist on disk if verbatim re-ingest is ever needed.

---

## Cutover Plan

**Critical lesson from Codex review:** Backfill BEFORE cutover. Never switch to Longhouse MCP with missing data.

### Sequencing

```
Phase 1: Build features (embeddings, insights, reservations)
  ── Foundation (sequential) ──
  1a. Fix Gemini raw_json gap in parser (moved from Phase 2 — stop compounding the gap)
  1b. Embedding config in models.json + models_config.py loader
  1c. SessionEmbedding model + needs_embedding column + import in database.py
  1d. Embedding client (Gemini default, OpenAI alt) + sanitize pipeline
  1e. Chunking pipeline (session_processing/embeddings.py)
  1f. Wire into ingest (background task, independent of summary success)
  1g. Backfill endpoint (POST /api/agents/backfill-embeddings)

  ── Parallel track A (depends on 1c-1g) ──
  1h. Embedding cache (in-memory numpy array, lazy-load turn-level)
  1i. Upgrade search_sessions MCP tool (add semantic mode, bypass FTS)
  1j. Add recall MCP tool (chunk-level search + event window retrieval)

  ── Parallel track B (independent of embeddings) ──
  1k. Insights table + API + MCP tools (log_insight, query_insights)
  1l. File reservations table + API + MCP tools (reserve/check/release)

  ── Tests ──
  1m. tests_lite/test_embeddings.py, test_insights.py, test_reservations.py, test_semantic_search.py

Phase 2: Backfill all data ← must complete before cutover
  2a. Backfill ~270 missing sessions from Life Hub (script)
  2b. Backfill embeddings for all ~10,700 sessions (bounded batches)
  2c. Migrate insights history from Life Hub (one-time script)

Phase 3: Verify + cutover
  3a. Dual-test: query both Life Hub and Longhouse, compare results
  3b. Update longhouse connect --install to register expanded MCP tools
  3c. Update CLAUDE.md global instructions to use Longhouse MCP
  3d. Remove Life Hub MCP from Claude Code config
  3e. Life Hub agents schema → read-only archive
```

**Phase 1** has two dependency chains:
- Foundation (1a→1g): sequential, each step builds on the prior
- Track A (1h→1j): depends on foundation, can parallel with Track B
- Track B (1k→1l): fully independent, can start immediately

**Phase 2** depends on Phase 1 being complete. Backfill before cutover is non-negotiable.
**Phase 3** depends on Phase 2 being complete. Zero-regression cutover.

---

## SQLite Considerations

From Codex review — real gotchas for this design:

1. **WAL mode required.** Embedding backfill + normal ingest = concurrent writes. WAL mode allows readers during writes. Longhouse should already be in WAL mode — verify.

2. **`busy_timeout` for write contention.** Background embedding tasks might collide with ingest writes. Set `busy_timeout=5000` (5 seconds) to retry instead of immediately failing.

3. **WAL bloat during backfill.** Inserting 350K turn embeddings (each ~1KB BLOB) will grow the WAL file. Run `PRAGMA wal_checkpoint(TRUNCATE)` periodically during backfill.

4. **In-memory embedding cache.** Don't decode BLOBs from SQLite on every search query. Load all session-level embeddings into a numpy array at startup (10MB at 256 dims). Invalidate on new ingest. For turn-level (340MB), lazy-load on first recall query and keep in memory.

5. **Single-worker assumption.** Longhouse runs single-process (uvicorn, 1 worker). In-memory cache works because there's one process. If multi-worker is ever needed, switch to memmap file.

---

## Decisions Log

| Decision | Rationale |
|----------|-----------|
| 256 dims, not 768/1536 | 97% quality at 8% storage. OpenAI uses 256-dim in their own file_search product. |
| Gemini default, not OpenAI | Free on personal Pro sub. OpenAI is employer-billed. OSS users configure their own. |
| Brute-force numpy, not sqlite-vec | sqlite-vec is pre-v1, brute-force only, adds C extension dep for zero benefit at this scale. |
| No `query_agents` raw SQL | Security risk in SQLite (ATTACH). Semantic search + FTS5 + structured API covers all use cases. |
| No session scoring/labels | David-specific quality scoring. No OSS value. Two extra tables + labeling workflow for marginal insight. |
| No task dependencies | Postgres-specific view in Life Hub. Basic tasks suffice. Easy to add later if needed. |
| No task MCP tools | Longhouse already has equivalent Oikos builtin tools. |
| `needs_embedding` flag, not job queue | Simpler than a job table. BackgroundTask + flag + backfill endpoint = eventual consistency. |
| Cleanup reservations on write, not cron | Same pattern as Life Hub. Opportunistic cleanup avoids needing a scheduled job. |
| Hybrid retrieval (FTS + rerank) | Codex suggested FTS top-K → embedding rerank. Good idea for graceful degradation when embeddings are missing. Implement in search_sessions: FTS first, rerank with embeddings if available. |
| Non-null project on reservations | SQLite allows multiple NULLs in unique indexes. Use empty string default instead. |
| Sanitize before embedding | Reuse strip_noise() + redact_secrets() from content.py. Prevents recall from surfacing secrets. |

---

## Files That Will Change

### New files
- `zerg/services/session_processing/embeddings.py` — embedding client + chunking pipeline
- `zerg/models/work.py` — Insight + FileReservation models
- `zerg/routers/insights.py` — insight API endpoints
- `zerg/routers/reservations.py` — reservation API endpoints
- `scripts/backfill_from_lifehub.py` — one-time migration script

### Modified files
- `zerg/models/agents.py` — add `SessionEmbedding` model + `needs_embedding` column on `AgentSession`
- `zerg/mcp_server/server.py` — add `recall`, `log_insight`, `query_insights`, `reserve_file`, `check_reservation`, `release_reservation`; upgrade `search_sessions` with semantic mode
- `zerg/routers/agents.py` — add `backfill-embeddings` endpoint; wire embedding generation into ingest background chain
- `zerg/services/shipper/providers/gemini.py` — fix `raw_line=""` → store actual JSON
- `config/models.json` — add `embedding` section

### Test files
- `tests_lite/test_embeddings.py` — embedding pipeline unit tests
- `tests_lite/test_insights.py` — insights CRUD + dedup
- `tests_lite/test_reservations.py` — reservation lifecycle
- `tests_lite/test_semantic_search.py` — search + recall integration

---

## Codex Review Findings (Addressed)

Issues identified during code-level review (Codex read the actual codebase files). All are incorporated into the implementation plan.

### Critical (fix during implementation)

1. **Embedding generation decoupled from summary success.** Summary generation in `routers/agents.py` skips when LLM is disabled. Embedding pipeline must independently check `needs_embedding` — don't chain off summary completion. Use backfill endpoint as catch-all.

2. **`needs_embedding` column default.** Adding boolean column must use `server_default=true` (SQLAlchemy) and run `UPDATE agent_sessions SET needs_embedding = 1 WHERE needs_embedding IS NULL` in `database.py` table creation or a migration block. Otherwise existing rows are NULL and skipped by backfill.

3. **Semantic search bypass FTS.** `search_sessions` MCP tool currently routes through FTS in `agents_store.py`. When `semantic=true`, bypass FTS entirely — query the embedding cache directly, return session IDs, then fetch metadata. Separate code path, not a flag on the existing FTS query.

### High (address in implementation)

4. **Embedding config loader.** `models_config.py` only loads `text` providers. Add `get_embedding_config()` function that reads `config/models.json` embedding block. Return provider, model, dims, API key env var. Fall back to "no embeddings" if unconfigured (graceful degradation for OSS users without keys).

5. **File reservation NULL project.** Use `server_default=""` for `project` column (non-nullable, empty string). Avoids SQLite's NULL-in-unique-index quirk. `reserve_file` MCP tool coerces `None` → `""`.

6. **Sanitize before embedding.** Call `strip_noise()` and `redact_secrets()` from `session_processing/content.py` on text before embedding. Prevents recall from surfacing API keys or tokens.

### Medium (address in implementation)

7. **Recall chunk mapping.** Store `event_index_start` and `event_index_end` on `SessionEmbedding` for turn-level chunks. Maps chunk back to specific events for context window retrieval.

8. **SessionEmbedding upsert.** Use `ON CONFLICT (session_id, kind, chunk_index, model) DO UPDATE SET embedding = excluded.embedding, content_hash = excluded.content_hash, created_at = excluded.created_at`. Re-embedding replaces in-place.

9. **Import new models in database.py.** Add `import zerg.models.work` in `database.py` so `Base.metadata.create_all` creates insights + reservations tables.

10. **Gemini token estimation.** Add `estimate_tokens_gemini(text) -> int` in `session_processing/tokens.py`: `len(text) // 3` (conservative). Use tiktoken for OpenAI, char-estimate for Gemini.

### Sequencing corrections

11. **Gemini raw_json fix → Phase 1** (moved from Phase 2). Prevents compounding the gap during implementation.

12. **Embedding config must land before embedding client code.** 1b depends on config/models.json + models_config.py changes.

13. **Don't upgrade search_sessions MCP tool until embedding cache is available and warm.** 1e depends on 1a-1d being complete.

---

## Open Questions (Reduced)

1. **Gemini free tier rate limits.** For 10K session backfill, how many embeddings/minute can we push? May need to spread over hours. Test empirically.
2. **Cursor parser.** Longhouse has 0 cursor sessions. Does a parser exist in the shipper, or does it need to be built for the ~230 cursor session backfill? (Check `shipper/providers/` for cursor support.)
3. **Embedding model migration.** If we later switch from Gemini 256-dim to OpenAI 256-dim (or change dims), all embeddings need re-computing. The `model` + `dims` columns on `SessionEmbedding` + the `needs_embedding` flag make this a backfill, not a schema change. Acceptable.
