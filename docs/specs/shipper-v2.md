# Shipper v2: Spec & Implementation Plan

**Status:** Draft
**Author:** David + Claude (research), Codex (algorithm design)
**Date:** 2026-02-15

## Problem Statement

The Longhouse shipper daemon (`longhouse connect`) ships AI session logs from user machines to Longhouse instances. The current implementation has a critical data amplification bug that grew a 9.8GB source directory into a 200GB spool file, crashing a MacBook.

**Root cause:** The JSONL parser fans out each source line into N events (one per content item: text, tool_use, tool_result), and attaches the **entire raw JSONL line** to **every** event. A 10MB line with 5 content items becomes 50MB. This 5-20x expansion is then stored uncompressed in a SQLite spool that has no size bounds and never cleans pending items.

**Secondary issues:** Non-atomic state file writes (crash → state loss → full re-spool), no backpressure, CPU-intensive SQLite usage pattern (new connection per operation).

## Design Goals

1. **Zero data amplification** — output payload ≤ source data size
2. **Bounded resource usage** — hard caps on spool size, memory, CPU
3. **Crash-safe** — no state loss on daemon kill/crash
4. **Minimal client footprint** — this runs 24/7 on user machines
5. **Backward compatible** — same ingest API, existing sessions unaffected

## Non-Goals (this spec)

- Rust rewrite (Phase 2, separate spec)
- API v2 protocol changes
- Changes to server-side ingest/storage

---

## Phase 1: Python Fix (Immediate — unblock shipper restart)

### 1.1 Fix raw_json fan-out

**Problem:** `parser.py` lines 153, 170, 225, 303 — every `ParsedEvent` yielded from a single JSONL line carries `raw_line=raw_line` (the entire source line).

**Fix:** Only the **first** event from each JSONL line carries `raw_json`. Subsequent events from the same line set `raw_json=None`.

**Implementation:**
- In `_extract_assistant_events()`: pass `raw_line` only to the first yielded event (track with a flag)
- In `_extract_tool_results()`: same pattern
- In `parse_session_file()`: if a line yields both assistant and tool_result events, only the first gets `raw_line`
- The server already deduplicates by `(source_path, source_offset)` — see `agents_store.py:565-605`

**Files:** `parser.py`
**Tests:** Update `test_parser.py` — verify only first event per line has `raw_json`

### 1.2 Eliminate the payload spool

**Problem:** On ship failure, the entire expanded payload (events + metadata) is serialized as uncompressed JSON TEXT into SQLite. This IS the 200GB.

**Fix:** Don't spool payloads. Spool **pointers** (file path + byte range). Re-read and re-parse from source on retry.

**New spool schema:**
```sql
CREATE TABLE spool_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    file_path TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    retry_count INTEGER DEFAULT 0,
    next_retry_at TEXT,          -- for exponential backoff
    last_error TEXT,
    status TEXT DEFAULT 'pending' -- pending|inflight|failed|dead
);
CREATE INDEX idx_spool_status ON spool_queue(status, next_retry_at);
```

**Max spool rows:** 10,000 (configurable). If exceeded, stop enqueueing — this is backpressure. Don't advance the offset either, so data will be picked up on next successful cycle.

**Retry with backoff:** `next_retry_at = now + min(base * 2^retry_count, 1 hour)`. Dequeue only fetches rows where `next_retry_at <= now`.

**Dead after:** 50 retries or 7 days old → status='dead', cleaned on next cycle.

**Files:** `spool.py` (rewrite), `shipper.py` (update enqueue/replay calls)
**Tests:** Update `test_spool.py`, `test_shipper.py` spool integration tests

### 1.3 Crash-safe state: merge into SQLite

**Problem:** `state.py` uses `open("w") + json.dump()` — non-atomic. Crash during write corrupts state → all offsets lost → full re-spool.

**Fix:** Move state into the same SQLite DB as the spool. SQLite is crash-safe by default (WAL mode).

**New state schema (same DB as spool):**
```sql
CREATE TABLE file_state (
    path TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    queued_offset INTEGER NOT NULL DEFAULT 0,   -- highest byte range queued/shipped
    acked_offset INTEGER NOT NULL DEFAULT 0,    -- highest byte range confirmed by server
    session_id TEXT,
    provider_session_id TEXT,
    last_updated TEXT NOT NULL
);
```

**Dual offsets:**
- `queued_offset`: advanced when bytes are enqueued to spool (or shipped directly)
- `acked_offset`: advanced only after server confirms receipt
- On startup, any range between `acked_offset` and `queued_offset` is re-queued (incomplete shipments from last run)

**Migration:** On first run, import existing `zerg-shipper-state.json` into SQLite, then delete the JSON file.

**Files:** `state.py` (rewrite to use SQLite), `spool.py` (shared DB)
**Tests:** Update `test_state.py`

### 1.4 Connection pooling

**Problem:** Every spool/state operation opens a new `sqlite3.connect()`. With thousands of operations per replay cycle, this is wasteful.

**Fix:** Single connection per `OfflineSpool`/`ShipperState` instance, with proper close on shutdown. Use WAL mode for concurrent read/write.

**Files:** `spool.py`, `state.py`

### 1.5 Bounded batch sizes

**Problem:** `ship_session()` reads ALL new events from a file into memory at once. A 993MB file produces a 993MB+ payload.

**Fix:** Batch by byte range. Max batch size: 5MB of source data (configurable). If a file has 100MB of new content, ship it in ~20 batches.

**Implementation:**
- `ship_session()` reads in chunks up to `max_batch_bytes`
- Each chunk gets its own ingest POST
- Offset advanced per successful chunk, not per file
- On failure, only the failed chunk is spool-queued

**Files:** `shipper.py`
**Tests:** Add test for large file batching in `test_shipper.py`

### 1.6 Fix replay_spool cleanup

**Problem:** `cleanup_old()` only runs at line 585 (end of `replay_spool()`), which is unreachable when ConnectError causes early return at line 544.

**Fix:** Move cleanup to `_spool_replay_loop()` in `connect.py` — call it unconditionally after each replay attempt. Also clean 'pending' items older than 7 days (currently only cleans 'shipped'/'failed').

**Files:** `connect.py`, `spool.py`

---

## Phase 2: Rust Rewrite (Medium-term)

### Rationale

The shipper runs 24/7 on user machines. Current Python daemon:
- Requires Python + venv + all backend dependencies (~200MB)
- Uses 50-100MB RSS minimum
- Slow startup (imports the entire zerg backend)
- Not crash-safe without careful coding

A Rust daemon would be:
- Single binary, 3-8MB (stripped)
- ~5-10MB RSS
- Instant startup
- Crash-safe by default (no GC, no interpreter state)

### Proposed Rust Stack (sync, minimal)

| Crate | Purpose | Why |
|-------|---------|-----|
| `notify` | Filesystem watching | Production-proven, native FSEvents/inotify |
| `serde` + `serde_json` | JSON/JSONL parsing | Standard, zero-copy capable |
| `ureq` | HTTP client (sync) | Minimal deps, no Tokio |
| `flate2` | Gzip compression | For HTTP payloads |
| `rusqlite` | SQLite state + spool | Production-proven, crash-safe |
| `signal-hook` | Graceful shutdown | SIGTERM/SIGINT handling |

No Tokio. No async. Straight-line code: watch → read → parse → gzip → POST → update SQLite.

### Scope

The Rust binary replaces ONLY the core engine:
- File discovery (provider abstraction for Claude/Codex/Gemini paths)
- JSONL/JSON parsing
- Incremental offset tracking (SQLite)
- HTTP shipping with gzip + retry
- Pointer-based spool (SQLite)
- File watching + polling fallback
- launchd/systemd service management

The Python CLI (`longhouse auth`, `longhouse recall`, hooks, MCP registration) stays Python. The Rust binary is invoked as `longhouse-engine connect --url ... --token ...`.

### Binary distribution

- GitHub Releases: `longhouse-engine-{version}-{target}.tar.gz`
- Targets: `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`
- `longhouse connect --install` downloads the correct binary

### Migration

- Python shipper remains as fallback (`longhouse connect --python`)
- Rust engine uses same SQLite DB schema (forward-compatible)
- First run migrates JSON state file to SQLite (same as Phase 1)

---

## Implementation Plan (Phase 1)

### Task Breakdown

| # | Task | Files | Blocked By | Est. Complexity |
|---|------|-------|------------|-----------------|
| 1 | Fix raw_json fan-out in parser | `parser.py`, `test_parser.py` | — | Small |
| 2 | Unified SQLite DB module (state + spool) | `spool.py` (rewrite), `state.py` (rewrite) | — | Medium |
| 3 | Pointer-based spool with backoff | `spool.py`, `test_spool.py` | #2 | Medium |
| 4 | Update ShipperState to use SQLite | `state.py`, `test_state.py` | #2 | Medium |
| 5 | Migrate JSON state on first run | `state.py` | #4 | Small |
| 6 | Bounded batch shipping | `shipper.py`, `test_shipper.py` | #2, #3, #4 | Medium |
| 7 | Update shipper to use pointer spool | `shipper.py` | #3 | Medium |
| 8 | Fix replay + unconditional cleanup | `connect.py`, `spool.py` | #3 | Small |
| 9 | Update `__init__.py` exports | `__init__.py` | #2-#8 | Small |
| 10 | Integration test: full pipeline | `test_smoke.py` | #1-#9 | Small |
| 11 | Fix shipper plist URL + kill legacy daemons | ops (not code) | — | Trivial |
| 12 | Trigger backfill on instance | ops (not code) | #11 | Trivial |

### Parallelization (SWM)

**Can run in parallel:**
- Task 1 (parser fix) — independent, no shared files
- Task 2+3+4 (SQLite module) — one teammate owns spool.py + state.py
- Task 11+12 (ops) — independent

**Must be sequential:**
- Tasks 6+7+8 depend on 2+3+4
- Task 9+10 are final integration

### Swarm partition:

| Teammate | Owns | Tasks |
|----------|------|-------|
| **parser-fix** | `parser.py`, `test_parser.py` | #1 |
| **spool-rewrite** | `spool.py`, `state.py`, `test_spool.py`, `test_state.py` | #2, #3, #4, #5 |
| **lead** (verifies, then does) | `shipper.py`, `connect.py`, `__init__.py`, `test_shipper.py`, `test_smoke.py` | #6, #7, #8, #9, #10 |
| **ops** (or lead) | plist, instance commands | #11, #12 |

---

## Server-Side Context (No Changes Needed)

The server ingest endpoint (`POST /api/agents/ingest`) already handles:
- Deduplication by `(session_id, source_path, source_offset, event_hash)` — safe to re-ship
- `raw_json` stored per-event row but only used during export (lossless resume)
- Only the export path (`GET /sessions/{id}/export`) reads `raw_json`, and it deduplicates by `(source_path, source_offset)` — so receiving `raw_json` on only the first event per line is fine

**Minimum required fields per event:** `role`, `timestamp`
**For summarization:** add `content_text`
**For full transcript:** add `tool_name`, `tool_input_json`, `tool_output_text`
**For lossless resume:** add `source_path`, `source_offset`, `raw_json` (once per line)

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Pointer spool: source file deleted before retry | Check file exists + inode matches before re-read. If gone, mark as dead. |
| SQLite migration: corrupt JSON state file | Best-effort import; if corrupt, start fresh (lose offsets = one-time full re-ship, but bounded by batch size) |
| Batch shipping: server gets partial session | Server already handles incremental ingest — subsequent batches append events |
| Large file memory usage | Stream-parse with byte limit, don't materialize all events |
| Existing tests break | Phase 1 changes internal APIs; all test files in task scope |

---

## Success Criteria

1. Shipper runs for 7 days without spool exceeding 100MB
2. CPU usage < 1% idle, < 5% during active shipping
3. Memory usage < 50MB RSS
4. All existing tests pass (updated for new APIs)
5. `make test` passes
6. Sessions ship within 30s of creation (watch mode)
7. Graceful degradation: if API is down for 24h, spool stays bounded, daemon stays healthy
