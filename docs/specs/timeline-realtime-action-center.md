# Timeline Realtime Desktop Control View

Status: In progress
Last updated: 2026-03-21

## Goal

Make `/timeline` the primary desktop runtime/control view for current agent sessions. Keep the existing single scrolling list, but make every row capable of answering:

- is this session active right now?
- what phase is it in?
- is it waiting on me, blocked, or quietly running?
- how recently did real progress happen?

The product target is still the same: replace terminal-tab juggling for local Claude and Codex work while keeping Timeline as the primary Longhouse desktop surface.

Timeline owns runtime observability and the desktop entry point. It does not define the canonical follow-up execution model, continuation transport, or mobile action semantics.

## Current Status

### Phase 1 shipped

Phase 1 is already in `main`:

- Timeline cards render live runtime strips directly on the main page.
- The backend no longer anchors recency on raw `started_at`; it now uses `timeline_anchor_at`.
- The main cards no longer rely on `!ended_at` to decide whether a session is active.

This shipped in:

- `4ee1ff29` `Fold live runtime state into timeline cards`
- `a94c990c` `Anchor timeline recency on live session activity`

### What Phase 1 did not solve

- Runtime truth is still computed ad hoc from presence + transcript data on read.
- Timeline still polls instead of receiving tiny runtime patches.
- Claude runtime semantics are richer than Codex runtime semantics.
- There is still no unified runtime state store or local runtime event protocol.
- Long silent turns still degrade to heuristics.

Phase 2 is the bridge from “truthier polling UI” to a real runtime system.

## Invariants

- One page: `/timeline`
- One primary layout: the existing scrolling card list
- No separate desktop live destination
- Row ordering is driven by meaningful activity, not heartbeats
- Frontend renders runtime state; backend owns runtime truth
- Provider-specific raw signals are normalized before the UI sees them
- Timeline phase is not the same thing as action availability

## Boundary

This spec is about runtime truth and live row rendering. It does not define:

- what `Continue` means across source sessions, hosted sessions, or cloud takeover
- how approvals/follow-ups are executed
- a second attention queue or action protocol separate from Loop/follow-up cards

Timeline may surface follow-up state, counts, and links, but it should consume the broader action model rather than inventing a new one.

## Design Principles

### 1. Separate progress, liveness, and phase

These are different facts:

- `progress`: last real work, like transcript append or tool result
- `liveness`: whether the runtime still appears alive
- `phase`: `thinking`, `running`, `blocked`, `needs_user`, `idle`, `finished`

Any design that collapses them into one timestamp will lie.

Runtime phase also does not imply a unique action policy. For example, `needs_user` means the runtime will not auto-advance on its own; it does not decide whether the user can reply on the source session, trigger hosted takeover, or use some other follow-up path.

### 2. One reducer

All runtime sources feed one normalized reducer. Timeline rows read a single materialized runtime state, not a mix of raw tables or competing endpoints.

### 3. Stable ordering, fast chrome

- `timeline_anchor_at` controls sort position
- `last_live_at` controls pulse/freshness
- same-phase keepalives do not reshuffle the list

### 4. Provider-native semantics first

- Claude hooks are the best semantic source for local Claude
- Codex app-server or SDK is the best semantic source for managed Codex
- transcript watchers and process signals are fallback inputs, not the primary meaning layer

### 5. Explicit confidence

The product should distinguish:

- `live`
- `inferred`
- `stale`

Fake certainty will destroy trust faster than slightly conservative labels.

## Phase 2 Scope

Phase 2 introduces a provider-agnostic runtime subsystem with four pieces:

1. a local runtime event protocol
2. a durable runtime event log
3. a materialized `session_runtime_state` table
4. an SSE patch stream for Timeline

Phase 2 does not yet require full PID/process-tree monitoring. That remains a later layer.
Phase 2 also does not define Timeline-native continue/approve semantics. It only makes runtime state trustworthy and cheap to render.

## Runtime Identity

Use a stable `runtime_key` for all runtime events.

Rules:

- managed launches should allocate the `runtime_key` up front
- unmanaged shipped sessions may use the provider session id or existing hook session id as `runtime_key`
- `session_id` is the canonical Longhouse archive session id when known

Why this exists:

- some runtime signals arrive before a canonical archive row is fully resolved
- this avoids provider-specific conditional joins everywhere

## Storage

### `session_runtime_state`

One row per live or recently-live runtime.

```sql
CREATE TABLE session_runtime_state (
  runtime_key TEXT PRIMARY KEY,
  session_id TEXT NULL,
  provider TEXT NOT NULL,
  device_id TEXT NULL,
  phase TEXT NOT NULL,
  phase_source TEXT NOT NULL,
  active_tool TEXT NULL,
  phase_started_at DATETIME NULL,
  last_runtime_signal_at DATETIME NULL,
  last_progress_at DATETIME NULL,
  last_live_at DATETIME NULL,
  timeline_anchor_at DATETIME NOT NULL,
  freshness_expires_at DATETIME NULL,
  terminal_state TEXT NULL,
  terminal_at DATETIME NULL,
  runtime_version INTEGER NOT NULL DEFAULT 0,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Indexes:

- `INDEX ix_runtime_state_session_id (session_id)`
- `INDEX ix_runtime_state_anchor (timeline_anchor_at DESC)`
- `INDEX ix_runtime_state_updated (updated_at DESC)`
- `INDEX ix_runtime_state_device_provider (device_id, provider)`

Field semantics:

- `phase`: current semantic runtime phase
- `phase_source`: `semantic`, `progress`, or `fallback`
- `last_runtime_signal_at`: last semantic/provider signal
- `last_progress_at`: last transcript/tool/message progress
- `last_live_at`: what the UI should use for “seen X ago”
- `timeline_anchor_at`: what ordering uses
- `freshness_expires_at`: when `live` should degrade
- `terminal_state`: `finished`, `interrupted`, `lost`, `crashed`
- `runtime_version`: monotonic counter for SSE patch ordering
- `confidence` is derived at read time from `freshness_expires_at`, `terminal_state`, and `last_progress_at`

### `session_runtime_events`

Append-only runtime signal log. This is primarily for debugging, replay, and deterministic reducer behavior.

```sql
CREATE TABLE session_runtime_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  runtime_key TEXT NOT NULL,
  session_id TEXT NULL,
  provider TEXT NOT NULL,
  device_id TEXT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  phase TEXT NULL,
  tool_name TEXT NULL,
  occurred_at DATETIME NOT NULL,
  freshness_ms INTEGER NULL,
  dedupe_key TEXT NOT NULL,
  payload_json TEXT NULL,
  received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Indexes and constraints:

- `UNIQUE INDEX ux_runtime_events_source_dedupe (source, dedupe_key)`
- `INDEX ix_runtime_events_runtime_key_occurred (runtime_key, occurred_at DESC)`
- `INDEX ix_runtime_events_session_id_occurred (session_id, occurred_at DESC)`

Retention:

- keep 7 to 14 days online
- prune older rows after state has been safely materialized

## Canonical Event Model

Do not store provider event names directly as product truth. Normalize into four event kinds.

### 1. `phase_signal`

Used when the provider or launcher knows the semantic state.

Fields:

- `phase`
- `tool_name`
- `freshness_ms`

Examples:

- Claude `UserPromptSubmit` -> `phase_signal(thinking)`
- Claude `PreToolUse` -> `phase_signal(running, tool=bash)`
- Claude `PermissionRequest` -> `phase_signal(blocked, tool=Edit)`
- Codex app-server `turn/started` -> `phase_signal(thinking)`
- Codex app-server `tool started` equivalent -> `phase_signal(running, tool=...)`

### 2. `progress_signal`

Used when we know real work happened but do not necessarily know the semantic phase.

Fields:

- `payload.progress_kind`

Allowed `progress_kind` values:

- `assistant_message`
- `tool_result`
- `transcript_append`
- `summary_ready`

### 3. `terminal_signal`

Used when a runtime definitively ends or becomes lost.

Fields:

- `payload.terminal_state`
- `payload.reason`

Allowed `terminal_state` values:

- `finished`
- `interrupted`
- `lost`
- `crashed`

### 4. `binding_signal`

Used to bind `runtime_key` to the canonical archive `session_id`.

Fields:

- `session_id`

This lets runtime state exist before or alongside transcript ingest without requiring brittle lookup logic at query time.

## Local Collector Protocol

All local adapters should emit the same JSON shape into the local runtime outbox.

```json
{
  "runtime_key": "claude:5db3...",
  "session_id": null,
  "provider": "claude",
  "device_id": "cinder",
  "source": "claude_hook",
  "kind": "phase_signal",
  "phase": "running",
  "tool_name": "bash",
  "occurred_at": "2026-03-21T20:15:02Z",
  "freshness_ms": 600000,
  "dedupe_key": "claude_hook:5db3:PreToolUse:2026-03-21T20:15:02Z",
  "payload": {}
}
```

Collector rules:

- hot paths never make network calls directly
- adapters write tiny JSON files or buffered batches to a local outbox
- the engine drains that outbox and ships batches
- ingestion must be idempotent on `(source, dedupe_key)`

Initial adapters:

- `claude_hook_adapter`
- `transcript_progress_adapter`
- `runtime_binding_adapter`

Phase-2 managed adapters:

- `codex_app_server_adapter`
- `managed_launch_adapter`

Not in Phase 2:

- full process census
- child-process tree tracking
- OS-wide heuristics for arbitrary unmanaged PIDs

## Reducer Rules

The reducer updates `session_runtime_state` from `session_runtime_events`.

### On `phase_signal`

- update `phase`
- update `phase_source = semantic`
- update `active_tool`
- update `last_runtime_signal_at`
- update `last_live_at`
- set `freshness_expires_at = occurred_at + freshness_ms`
- if the phase changed, set `phase_started_at = occurred_at`
- update `timeline_anchor_at` only when:
  - phase changed from non-live to live
  - phase changed to `blocked`
  - phase changed to `needs_user`
  - phase changed to a terminal attention state
- increment `runtime_version` if any material field changed

### On `progress_signal`

- update `last_progress_at`
- update `timeline_anchor_at = occurred_at`
- if there is no fresh semantic phase and the runtime is not terminal:
  - set `phase_source = progress`
  - keep current phase if still meaningful
  - otherwise let the UI render a generic active state through `confidence = inferred`
- increment `runtime_version`

### On `terminal_signal`

- set `terminal_state`
- set `terminal_at`
- set `phase = finished`
- clear `active_tool`
- set `confidence = stale`
- update `timeline_anchor_at = occurred_at`
- increment `runtime_version`

### On `binding_signal`

- set `session_id`
- do not change `timeline_anchor_at`
- increment `runtime_version` only if binding changed

### Read-time degradation

At query or patch-build time:

- if `freshness_expires_at > now` -> `confidence = live`
- else if `terminal_state IS NULL` and `now - last_progress_at <= 5m` -> `confidence = inferred`
- else -> `confidence = stale`

This keeps the reducer simple while still allowing phase-specific TTLs.

## Freshness Windows

Default values for Phase 2:

- `thinking`: 90 seconds
- `running`: 10 minutes
- `idle`: 10 minutes
- `blocked`: 24 hours
- `needs_user`: 24 hours
- transcript-only inference: 5 minutes

Why:

- `thinking` should go stale quickly if nothing else happens
- `running` needs a longer leash because tools can be quiet
- `blocked` and `needs_user` are attention states, not “live execution” states

These windows should be constants in one shared module, not repeated across handlers.

## API Contracts

### 1. Runtime ingest

`POST /api/agents/runtime/events/batch`

Request:

```json
{
  "events": [
    {
      "runtime_key": "claude:5db3...",
      "session_id": null,
      "provider": "claude",
      "device_id": "cinder",
      "source": "claude_hook",
      "kind": "phase_signal",
      "phase": "running",
      "tool_name": "bash",
      "occurred_at": "2026-03-21T20:15:02Z",
      "freshness_ms": 600000,
      "dedupe_key": "claude_hook:5db3:PreToolUse:2026-03-21T20:15:02Z",
      "payload": {}
    }
  ]
}
```

Response:

```json
{
  "accepted": 1,
  "duplicates": 0,
  "updated_runtime_keys": ["claude:5db3..."]
}
```

Requirements:

- batch size limit: 128 events
- max body size: 128 KB
- idempotent
- safe to replay after offline periods

### 2. Timeline list

`GET /api/timeline/sessions`

Keep the current row shape stable, but add canonical runtime fields:

- `runtime_phase`
- `phase_started_at`
- `last_progress_at`
- `runtime_source`
- `terminal_state`
- `runtime_version`

Keep current compatibility fields for the UI:

- `status`
- `presence_state`
- `presence_tool`
- `presence_updated_at`
- `last_live_at`
- `display_phase`
- `active_tool`
- `confidence`
- `timeline_anchor_at`

Join rules:

- join `AgentSession` to `session_runtime_state` via `session_id` when present
- fallback join via `runtime_key` only while migration is in flight

Ordering:

- `timeline_anchor_at DESC`
- tie-breaker `started_at DESC`

### 3. Timeline SSE

`GET /api/timeline/sessions/stream`

Query params should mirror the normal Timeline list filters:

- `project`
- `provider`
- `environment`
- `days_back`
- `hide_autonomous`
- `sort`

Event types:

#### `connected`

```json
{
  "server_time": "2026-03-21T20:15:02Z"
}
```

#### `session_patch`

```json
{
  "session_id": "34d4...",
  "runtime_version": 18,
  "changes": {
    "status": "working",
    "runtime_phase": "running",
    "active_tool": "bash",
    "display_phase": "Running bash",
    "last_live_at": "2026-03-21T20:15:02Z",
    "confidence": "live",
    "timeline_anchor_at": "2026-03-21T20:15:02Z"
  },
  "reorder": false
}
```

#### `session_insert`

Sent when a runtime change causes a row to newly enter the active result window.

Payload:

- full session row

#### `session_remove`

```json
{
  "session_id": "34d4...",
  "reason": "fell_out_of_window"
}
```

#### `resync_required`

Used when the server decides a patch stream is no longer safe to apply incrementally.

Payload:

- `reason`

#### `heartbeat`

Empty or timestamp-only keepalive.

Client behavior:

- apply `session_patch` idempotently using `runtime_version`
- refetch full `/timeline/sessions` on `resync_required`
- do not reorder on every patch unless `reorder=true`

Server behavior:

- when a row has `freshness_expires_at`, schedule a synthetic stale patch at that timestamp
- that patch should update `confidence` and any derived display fields without changing `timeline_anchor_at`
- this keeps confidence server-authored without introducing heartbeat-driven reordering

## Provider Adapters

### Claude

Phase 2 path:

- keep existing hooks
- stop treating `/agents/presence` as the product truth store
- translate hook payloads into `phase_signal` runtime events
- continue using transcript shipping for `progress_signal`

This keeps the hot path local and preserves the current Claude integration.

### Codex

Phase 2 path for managed launches:

- launch behind Codex app-server or SDK
- emit `phase_signal` and `progress_signal` directly from managed runtime events

Fallback for unmanaged sessions:

- transcript watcher emits `progress_signal`
- runtime confidence is usually `inferred` unless stronger signals exist

This is the point where Codex stops being second-class for realtime semantics, but only for managed paths.

## Frontend Model

Timeline still renders one list, but the browser should treat rows as:

- durable historical session context
- fast-changing runtime overlay

Client storage needs:

- row map by `session_id`
- current order list
- `runtime_version` guard per row

Do not do:

- full-list refetch every 2 seconds forever
- client-side truth merging between two unrelated feeds as the long-term design
- re-sorting on every freshness tick

## Migration Plan

### Step 1

- add `session_runtime_state`
- add `session_runtime_events`
- add reducer service

### Step 2

- add runtime event ingest endpoint
- translate Claude hook presence into runtime events
- emit `binding_signal` and `progress_signal` from transcript ingest

### Step 3

- make `/timeline/sessions` read the materialized runtime state instead of ad hoc presence joins
- keep current response compatibility fields intact

### Step 4

- add `/timeline/sessions/stream`
- patch the existing Timeline page with SSE updates
- keep polling as a fallback only

### Step 5

- add managed Codex adapter
- collapse the separate active/live endpoint out of the Timeline critical path

## What Waits Until Later

- full PID/process-tree monitoring
- OS sleep/wake inference
- child-process tool census
- global process scanning for unmanaged sessions
- aggressive low-latency lease protocols

Those are valuable, but they should layer onto Phase 2 rather than block it.

## Simpler Cut If Needed

If Phase 2 starts feeling too heavy, cut it down to:

- `session_runtime_state`
- runtime ingest endpoint
- no durable event retention beyond a tiny rolling log
- SSE patches

That is still much better than the current ad hoc runtime derivation, and it avoids building a giant system before it earns its keep.

## Better-Than-Phase-2 Upgrade

If we want to one-up this design later without making the user-facing model more complicated:

- Longhouse-owned launch wrappers allocate canonical runtime keys up front
- managed runtimes emit short leases in addition to semantic events
- the timeline debug inspector shows exactly why each card is labeled the way it is

That would let us shrink freshness windows, improve Codex accuracy, and make “live” closer to authoritative truth without changing the one-page UX.
