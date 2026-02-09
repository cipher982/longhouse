# Unified Memory Bridge (Read-Through Dogfood)

**Status:** Draft
**Owner:** David Rose
**Last updated:** 2026-02-09

## Why this exists

Longhouse is the product we want to dogfood, but Life Hub is the system that actually works day-to-day. This spec defines a low-risk bridge so Longhouse can **read through** Life Hub for agent memory while we keep shipping, then cut over cleanly later.

The goal is to make Longhouse feel like the canonical memory UI **without forcing a big-bang migration**.

## Goals

- Longhouse UI and Oikos tools can query agent memory even if Life Hub remains canonical.
- No workflow break for current Life Hub users.
- Clear migration path to make Longhouse canonical later.
- Minimal surface area: use existing Life Hub API endpoints (not new DB links).

## Non-goals

- Multi-tenant support (still single-tenant).
- New UI for agent rooms (separate spec).
- Full semantic search in OSS on day one.

## Current state (verified in code)

### Local → Longhouse shipper

- Watches `~/.claude/projects/**/*.jsonl` and ships to `/api/agents/ingest`.
- Tracks byte offsets in `~/.claude/zerg-shipper-state.json`.
- Spools offline payloads to `~/.claude/zerg-shipper-spool.db`.

Refs: `zerg/services/shipper/shipper.py`, `zerg/services/shipper/state.py`, `docs/specs/shipper.md`.

### Longhouse session resume

- **Export** JSONL from DB → write to server `~/.claude/projects/...` → `claude --resume`.
- **Ship back** server-local JSONL to `/api/agents/ingest` so the DB has the new events.

Refs: `zerg/services/session_continuity.py`, `zerg/routers/session_chat.py`.

### Memory split today

- Longhouse: keyword search using SQLite FTS5 (`events_fts`).
- Life Hub: semantic search via embeddings + pgvector; MCP server exposes `search_agents`.

Refs: `zerg/services/agents_store.py`, `zerg/database.py`, `life_hub/mcp_server.py`.

## Problem statement

Longhouse and Life Hub both store agent logs, but there is no single canonical memory.
This blocks dogfooding because:

- Longhouse UI search is incomplete (no semantic search).
- Life Hub has strong memory but no Longhouse UX.
- Switching would require abandoning the current, working system.

## Decision: Read-through bridge

Longhouse should **read agent memory from Life Hub** when configured, while keeping the same Longhouse API surface for UI and tools.

This enables immediate dogfooding of the Longhouse UI without migration risk.

## Architecture (read-through)

### Diagram: read-through path

```
Longhouse UI / Oikos tools
          |
          v
   Longhouse API (/api/agents/*)
          |
          +--------------------+
          | AGENTS_BACKEND=local   -> Local DB (SQLite/Postgres)
          | AGENTS_BACKEND=life_hub -> Life Hub API (canonical)
          +--------------------+
```

### Diagram: current shipper + resume flows (for context)

```
Local Claude JSONL -> Shipper -> /api/agents/ingest -> Longhouse DB

Longhouse chat:
  export JSONL -> write server ~/.claude/... -> claude --resume -> re-ingest
```

## Interface proposal

Add a thin `AgentsBackend` abstraction in Longhouse:

```text
list_sessions(filters) -> {sessions, total}
get_session(session_id) -> session
get_session_events(session_id, filters) -> {events, total}
search_sessions(query, filters) -> {sessions, total, matches}
export_session_jsonl(session_id) -> (bytes, cwd, provider_session_id)
```

### Implementations

- `LocalAgentsBackend`: wraps existing `AgentsStore` (current behavior).
- `LifeHubAgentsBackend`: HTTP client to Life Hub endpoints.

### Config

- `AGENTS_BACKEND=local|life_hub` (default `local`)
- `LIFE_HUB_BASE_URL` (e.g. `https://data.drose.io`)
- `LIFE_HUB_API_KEY`

## Life Hub endpoints (confirmed)

From `life_hub/api/routers/agents.py`:

- `POST /ingest/agents/events` (write)
- `GET /query/agents/sessions`
- `GET /query/agents/sessions/{id}`
- `GET /query/agents/sessions/{id}/events`
- `GET /query/agents/search` (full-text)
- `GET /query/agents/sessions/{id}/export` (JSONL)
- `GET /api/agents/full/semantic-search` (semantic)

## Search behavior

- If backend = `local`: keep FTS5 keyword search (`events_fts`).
- If backend = `life_hub`: prefer semantic search when available.
  - First attempt: `/api/agents/full/semantic-search`
  - Fallback: `/query/agents/search` (FTS)

## Data mapping notes

Longhouse uses:
- `AgentSession`: `id`, `provider`, `project`, `cwd`, `git_repo`, `git_branch`, timestamps, counters.
- `AgentEvent`: `role`, `content_text`, `tool_name`, `tool_input_json`, `tool_output_text`, `timestamp`.

Life Hub returns similar fields but different envelopes (`data`, `count`).
Bridge should normalize to the existing Longhouse shapes.

## Migration plan

### Phase 0 — Read-through (now)

- Longhouse reads from Life Hub when `AGENTS_BACKEND=life_hub`.
- No migration, no data copying.
- Dogfood Longhouse UI immediately.

### Phase 1 — Write-through (optional)

- Shipper continues sending to Life Hub.
- Longhouse ingest optionally forwards to Life Hub (dual-write) for testing.

### Phase 2 — Backfill + Cutover

- Backfill Life Hub sessions into Longhouse DB.
- Switch `AGENTS_BACKEND=local`.
- Stop dual-write.

### Phase 3 — Simplify

- Remove Life Hub adapter paths when stable.

## Risks

- Semantic search availability (Life Hub depends on embeddings + vector index).
- Session export/resume must use the same backend (read-through adapter must handle export).
- Potential drift if local shipper and Life Hub diverge during cutover.

## Implementation checklist

- [ ] Add `AgentsBackend` interface + two implementations.
- [ ] Wire `/api/agents/*` router to backend.
- [ ] Wire session tools (`search_sessions`, `get_session_detail`, etc.) to backend.
- [ ] Wire session export for resume to backend.
- [ ] Add config + env docs.
- [ ] Add integration tests for both backends (smoke only).

## Open questions

1) Should Longhouse ever write directly to Life Hub (dual-write), or stay read-only during Phase 0?
2) Should MCP tools point to Longhouse once read-through is live?
3) Do we want semantic search in OSS (sqlite-vec) later, or keep it hosted-only?
