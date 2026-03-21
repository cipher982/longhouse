# Timeline Realtime Desktop Control View

Status: In progress
Last updated: 2026-03-21

## Goal

Make `/timeline` the primary desktop runtime/control view for current agent sessions.

Keep one page and one scrolling list, but make each row reliably answer:

- is this session active right now?
- what phase is it in?
- is it waiting on me, blocked, or quietly running?
- how recently did real progress happen?

Timeline owns desktop runtime observability. It does not define continuation transport, approval semantics, or the canonical follow-up action model.

## Invariants

- One page: `/timeline`
- One primary layout: the existing scrolling card list
- No separate desktop live destination
- Row ordering follows meaningful activity, not heartbeat noise
- Backend owns runtime truth; frontend renders it
- Timeline phase is not the same thing as action availability

## What Exists Now

These pieces already exist in code and are not future work:

- Materialized runtime state and runtime event storage back Timeline runtime truth.
- The backend reducer in `apps/zerg/backend/zerg/services/session_runtime.py` builds the runtime view used by Timeline rows.
- Session ordering uses `timeline_anchor_at` instead of raw `started_at`.
- Main Timeline cards render runtime state directly on the existing list.
- Timeline has a row-level SSE stream in `apps/zerg/backend/zerg/routers/timeline.py` using `session_upsert` and `session_remove`.
- The browser uses that stream with a slow reconciliation poll in `apps/zerg/frontend-web/src/pages/SessionsPage.tsx`.
- Background tabs now pause the Timeline SSE stream, and non-SSE clients stay on polling instead of silently going stale.

## Core Model

Keep these facts separate:

- `progress`: last real work, like a transcript append or tool result
- `phase`: `thinking`, `running`, `blocked`, `needs_user`, `idle`, `finished`
- `liveness`: whether the runtime still appears fresh
- `confidence`: `live`, `inferred`, `stale`

Any design that collapses these into one timestamp will lie.

## Current Architecture

### Backend

- `session_runtime_state` is the materialized runtime read model.
- `session_runtime_events` is the durable reducer input and debugging log.
- `/api/timeline/sessions` returns the Timeline row shape with runtime overlay fields already attached.
- `/api/timeline/sessions/stream` publishes row-level upserts/removes for the same filtered result window.

### Frontend

- The Timeline page uses `/timeline/sessions` as the durable list query.
- `useTimelineSessionStream()` applies live row updates on top of that list.
- A slow reconciliation poll remains in place as a backstop.

This is the right shape and should remain the default model.

## What Is Still Messy

Three migration leftovers remain:

1. The main Timeline cards still support optional merging with `/sessions/active` overlay data.
2. Timeline reads can still fall back to ad hoc runtime derivation instead of always trusting materialized runtime state first.
3. The SSE backend still polls the full filtered list every second and diffs serialized rows.

Those are the actual near-term cleanup items.

## Immediate Work

### 1. Collapse the main Timeline off `/sessions/active`

The main list should render from the Timeline row itself, not depend on a secondary live overlay feed.

Keep `/sessions/active` only for the optional live subpanel or other non-critical-path surfaces until it can be removed or narrowed.

### 2. Make materialized runtime state the clear primary truth path

The reducer-backed runtime state should be the normal source for Timeline reads.

Ad hoc fallback logic from presence + transcript can stay temporarily for migration safety, but it should be treated as compatibility code, not permanent architecture.

### 3. Replace the 1-second full-list SSE polling loop with a cheaper change detector

The current stream works, but the hot path in `apps/zerg/backend/zerg/routers/timeline.py` is still too expensive:

- it calls the full session listing path every second
- it serializes whole rows
- it diffs JSON signatures for each client

The next optimization should keep the simple `session_upsert` / `session_remove` protocol and make change detection cheaper before adding a richer stream protocol.

## Explicitly Deferred

Do not expand scope into these yet:

- PID/process-tree supervision
- richer SSE patch/resync machinery beyond row upsert/remove
- runtime-key complexity on the Timeline read path unless current session binding proves insufficient
- Timeline-native continue/approve semantics
- a separate desktop live page or second list model

## Clean Target From Here

- one authoritative row shape from `GET /api/timeline/sessions`
- one reducer in `apps/zerg/backend/zerg/services/session_runtime.py`
- one materialized runtime state source for Timeline reads
- one SSE stream publishing row-level upserts/removes for the same filtered window
- one browser store keyed by `session_id`
- no second live-truth feed in the Timeline critical path

If the simple row-level stream becomes too expensive even after cheaper change detection, then revisit richer patch/resync machinery. Not before.
