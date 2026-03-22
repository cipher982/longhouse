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

## Phased Plan

### Phase 1: Honest desktop semantics

Goal: make the current Timeline labels truthful without pretending unmanaged local sessions are exact.

Scope:

- Replace inferred `Active` language with weaker user-facing wording.
- Keep confidence explicit on rows, but stop making inference read like certainty.
- Tighten the row model around `phase`, `confidence`, and `runtime_source`.

Success criteria:

- Inferred unmanaged rows no longer render as plain `Active`.
- A user can visually distinguish authoritative live execution from recent-progress inference.
- Timeline cards do not imply that an open terminal tab means the agent is actively working.
- Backend and frontend tests lock the new label/state contract.

### Phase 2: Execution-home visibility

Goal: let Timeline answer where a session is running without defining what actions are available there.

Scope:

- Add explicit `execution_home` metadata to session APIs and Timeline rows.
- Show concise desktop labels like `On this Mac`, `Hosted`, or `Cloud`.
- Keep action semantics separate from this field.

Success criteria:

- Timeline rows expose a canonical execution-home field.
- The user can tell managed-local from legacy/unmanaged rows at a glance.
- The new field does not silently change continuation behavior or Loop semantics.

### Phase 3: Managed runtime class

Goal: treat managed sessions as a stronger runtime source than transcript-only fallback.

Scope:

- Introduce stronger runtime-source semantics for managed sessions.
- Prefer managed transport/runtime signals over transcript progress when available.
- Keep unmanaged local as fallback observability only.

Success criteria:

- Managed-local sessions can surface stronger runtime truth than legacy transcript-only sessions.
- Runtime-source precedence is explicit and test-covered.
- Timeline remains read-only with respect to continuation transport semantics.

### Phase 4: Cleanup and scale

Goal: finish the migration residue and make the hot path cheaper.

Scope:

- Collapse the main Timeline off `/sessions/active`.
- Make materialized runtime state the clear primary truth path.
- Replace the 1-second full-list SSE polling loop with a cheaper change detector.

Success criteria:

- Main Timeline cards render from `/timeline/sessions` rows only.
- Remaining ad hoc fallback paths are clearly compatibility-only.
- Timeline streaming no longer requires full filtered-list recompute/diff per client every second.
- Manual QA covers multiple concurrent sessions and long-running silent turns.

## Explicitly Deferred

Do not expand scope into these yet:

- exact working detection for unmanaged local Codex sessions
- `open terminal but idle` detection for unmanaged local sessions
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
