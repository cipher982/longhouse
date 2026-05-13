# Session Timing Model

Status: Proposed
Last updated: 2026-04-15

## Goal

Make Longhouse timing truth explicit across Claude, Codex, and imported sessions without turning the raw transcript archive into a muddy lifecycle model.

The product should be able to answer, with explicit confidence:

- when a session started and ended
- when a user turn was submitted
- when Longhouse first observed active execution for that turn
- when the turn reached a terminal phase
- when the assistant response became durable in the transcript
- which timing facts are exact vs reconstructed vs inferred

This model is machine-first. Any browser timer, status chip, or analytics view should consume this model rather than invent timing semantics in the UI.

## Why Now

Longhouse already stores:

- session-level timing on `sessions.started_at`, `sessions.ended_at`, and `sessions.last_activity_at`
- raw transcript event timing on `events.timestamp`
- live runtime state through `session_runtime_state`
- managed-local per-turn timing in the `managed_local_turns` shadow ledger

That is enough to build a narrow elapsed-session counter, but it is not yet a canonical timing model.

Today we do not have one provider-agnostic place to ask:

- what counts as a turn
- when a turn was submitted vs when active work was first observed
- when a turn finished vs when it became durable
- how timing truth differs for managed sessions vs imported sessions

Before launch, Longhouse should define that truth cleanly instead of accreting browser-only heuristics or per-provider sidecars.

## Current State

### What already exists

- `sessions` is the canonical session row.
- `events` is the canonical raw transcript archive.
- `session_runtime_state` carries the latest live/runtime overlay.
- `managed_local_turns` tracks managed-local prompt acceptance, terminal phase, and durable linkage.

### What is missing

- a canonical turn model for all managed sessions
- explicit confidence semantics for imported/reconstructed timing
- a machine-facing turns surface on `/api/agents/*`
- a clear separation between durable product truth and noisy live progress signals

### Existing shape we should preserve

- `events` should remain the lossless transcript/archive layer.
- `events.timestamp` should stay the primary archive timestamp for a raw event.
- browser and MCP surfaces should continue to sit on the canonical machine model instead of owning timing semantics themselves.

## Decision

Longhouse will add a canonical `session_turns` model and make it the product truth for turn timing.

Rules:

- `events` remains the raw transcript/archive layer.
- `session_turns` becomes the durable turn lifecycle layer.
- `session_runtime_state` remains the latest-state overlay, not the long-term analytics truth.
- high-frequency progress ticks must not become the canonical durable model.
- durations should be derived on read from timestamps, not stored as the primary truth.
- canonical writes for launch-scoped exact producer paths must not be best-effort side writes that silently fail.

This means:

- we do **not** add generic `started_at` / `finished_at` fields to every transcript event row
- we **do** store richer timing fields where they correspond to real lifecycle boundaries
- we **do** allow exact, partial, and inferred timing, but make confidence explicit
- we **do not** declare exact managed parity for providers until equivalent producer signals exist

## Timing Vocabulary

### Session

A session is the long-lived conversation/execution container already represented by `sessions`.

Canonical session fields:

- `started_at`
- `ended_at`
- `last_activity_at`

### Event

An event is one raw transcript entry in `events`.

Examples:

- user message
- assistant message chunk or completed message
- tool call
- tool result
- system event

Events are archive facts. They are not the primary lifecycle model.

### Turn

A turn is one user request and the agent work that follows from it until the turn reaches a terminal state.

A turn may contain:

- one user event
- many runtime signals
- many tool calls and tool results
- one or more assistant events

The turn is the unit that should answer timing questions for product UI, analytics, and coordination.

### Runtime signal

A runtime signal is a high-frequency liveness or phase update.

Examples:

- thinking
- running tool
- blocked
- needs user
- finished

Runtime signals are useful for current state, but should not be persisted forever as the only source of timing truth.

## Canonical Model

### New table: `session_turns`

Launch-minimum shape:

- `id`
- `session_id`
- `request_id`
- `source_kind`
- `timing_confidence`
- `state`
- `terminal_phase`
- `error_code`
- `user_event_id`
- `durable_assistant_event_id`
- `baseline_event_id`
- `baseline_observation_cursor`
- `user_submitted_at`
- `send_accepted_at`
- `active_phase_observed_at`
- `terminal_at`
- `durable_at`
- `created_at`
- `updated_at`

Deferred extension columns, only when a concrete producer exists:

- `ordinal`
- `assistant_event_start_id`
- `assistant_event_end_id`
- `first_output_at`
- `assistant_completed_at`
- `terminal_observation_id`

### Launch-minimum invariants

- one row per turn
- `session_id + request_id` should be unique when `request_id` is present
- `source_kind`, `timing_confidence`, `state`, `user_submitted_at`, `created_at`, and `updated_at` are required
- `send_accepted_at` must stay `NULL` until the transport acceptance milestone is real
- `active_phase_observed_at` must stay `NULL` until Longhouse observes a true runtime phase transition into `thinking` or `running`; transport acceptance alone is not sufficient
- `terminal_at` implies `state in {terminal, durable}` and `terminal_phase IS NOT NULL`
- `durable_at` implies `state = durable` and `durable_assistant_event_id IS NOT NULL`
- `terminal_observation_id` must be `NULL` unless a terminal runtime observation was recorded
- state transitions may not regress

### Launch-minimum schema notes

- `id`: `Integer` primary key with autoincrement
- `session_id`: `GUID()` foreign key to `sessions.id`, indexed, not null
- `request_id`: nullable `String(64)`, indexed
- `source_kind`: `String(32)`, not null
- `timing_confidence`: `String(20)`, not null
- `state`: `String(20)`, not null
- `terminal_phase`: nullable `String(32)`
- `error_code`: nullable `String(64)`
- `user_event_id`, `durable_assistant_event_id`, `baseline_event_id`, `baseline_observation_cursor`: nullable integers
- all timestamps: timezone-aware `DateTime(timezone=True)`

Launch-minimum indexes and constraints:

- unique partial constraint on `session_id + request_id` where `request_id IS NOT NULL`
- index on `session_id, created_at, id` for stable per-session listing
- optional secondary index on `session_id, state, created_at` if launch queries need it
- SQLite migration note: `session_turns` is a new `AgentsBase` table, so implementation must ensure both the table and its partial unique index are created for fresh installs and existing SQLite deploys; `_migrate_agents_columns()` is still required for new columns on existing tables, but it is not a substitute for creating this table-level index correctly

### Launch-minimum state machine

Valid transitions:

- `created -> send_accepted`
- `created -> failed`
- `send_accepted -> active`
- `send_accepted -> terminal`
- `send_accepted -> failed`
- `active -> terminal`
- `active -> failed`
- `terminal -> durable`

Rules:

- updates must be idempotent
- the same milestone may be written again only if it does not change the effective value
- later milestones may arrive without earlier optional milestones
- `durable` does not require `active`; imported or partially observed turns may go `created -> terminal -> durable`
- `failed` should only be written when the launch-scoped producer considers the turn irrecoverable; timeout or transient transport loss must not force a state that later requires regression

### Field intent

#### Identity

- `session_id`: owning session
- `request_id`: transport/control identifier when one exists

Deferred identity:

- `ordinal`: human-friendly per-session turn number, added only after there is a deterministic producer or backfill strategy that does not create write-time races

#### Provenance

- `source_kind`: where this timing record came from
- `timing_confidence`: whether the timing is exact, partial, or inferred

#### Transcript linkage

- `user_event_id`: durable transcript event for the triggering user prompt
- `durable_assistant_event_id`: durable assistant event currently used to mark turn completion
- `baseline_event_id`: latest durable event id before the turn began
- `baseline_observation_cursor`: latest runtime observation id before the turn began; a cursor, not a required FK-like link

Deferred linkage:

- `assistant_event_start_id`: first durable assistant event attributed to the turn
- `assistant_event_end_id`: last durable assistant event attributed to the turn
- `terminal_observation_id`: raw observation id for the terminal runtime signal when available

#### Timestamps

- `user_submitted_at`: when the user prompt was accepted as a turn
- `send_accepted_at`: when the transport acknowledged the prompt send
- `active_phase_observed_at`: when Longhouse first observed an active runtime phase for the turn
- `terminal_at`: when the turn reached its terminal phase
- `durable_at`: when the turn's durable transcript linkage was established

Deferred timestamps:

- `first_output_at`: only when a real producer can observe first output
- `assistant_completed_at`: only when Longhouse has a provider/runtime contract that distinguishes completion from terminal phase

### Enumerations

#### `source_kind`

- `managed_live`
- `imported_reconstructed`
- `imported_partial`

`managed_live` still applies when a managed turn later times out, retries, or reaches durability through recovery. Source provenance and timing quality are different concerns; degraded completeness belongs in `timing_confidence`, missing milestones, or `state`, not in a separate launch enum.

#### `timing_confidence`

- `exact`
- `partial`
- `inferred`

Definitions:

- `exact`: every populated timing field on the row came from a direct managed signal or direct durable transcript fact; no populated field was reconstructed heuristically
- `partial`: the row is backed by real signals or transcript facts, but one or more milestones are unavailable on that source path
- `inferred`: one or more populated timing fields were reconstructed rather than directly observed

#### `state`

- `created`
- `send_accepted`
- `active`
- `terminal`
- `durable`
- `failed`

The state tracks observed lifecycle milestones, not raw provider phase text.
`terminal_phase` carries the user-facing terminal distinction such as `idle`, `needs_user`, or `blocked` when that distinction is known.

## What stays out of `session_turns`

These do **not** belong in the canonical turn table:

- every 1s or 10s progress heartbeat
- every runtime phase update forever
- precomputed duration columns as the primary truth
- provider-specific transport payload blobs that are only useful for debugging

Longhouse already retains runtime observations as the substrate for runtime-state materialization and recency anchoring. The design rule is that observations remain substrate, not the primary turn-truth model. The optional question is long-term retention depth beyond current operational needs.

## Derived Durations

Durations should be computed from timestamps on read.

Examples:

- submit-to-send = `send_accepted_at - user_submitted_at`
- submit-to-active = `active_phase_observed_at - user_submitted_at`
- active-to-terminal = `terminal_at - active_phase_observed_at`
- terminal-to-durable = `durable_at - terminal_at`
- total-turn-time = `(durable_at or terminal_at) - user_submitted_at`

This keeps the stored truth minimal and avoids drift between timestamps and cached duration fields.

Cached duration fields may be added later for performance if a measured query path needs them. They are optimization artifacts, not the source of truth.

## Canonical Write Contract

The launch-scoped canonical write path must not inherit the current best-effort shadow-write behavior.

Rules:

- `session_turns` writes must not go through `run_best_effort_managed_local_turn_write`
- initial turn-row creation is part of the owning managed request path; if that insert fails, the request fails
- `send_accepted_at` is part of the same launch-scoped managed request path; if the canonical update cannot be recorded before returning success, the request fails
- later milestones (`active`, `terminal`, `durable`) may be observed by separate runtime or ingest paths, but failures must be explicit and retriable rather than silently swallowed
- those later milestone writes should follow the same SQLite write-safety discipline as current high-frequency runtime and presence paths, using `WriteSerializer` or an equivalent serialized write seam where appropriate
- migration may keep `managed_local_turns` as a best-effort secondary write only after the canonical `session_turns` write succeeds

This yields one hard guarantee for launch:

- Longhouse never reports a managed send as accepted while silently missing the canonical turn row that should own that send

## Why Not Put Start/Stop On Every Event

Longhouse already writes every transcript event, so adding more timestamp columns is not scary from a storage perspective.

The problem is semantic, not bytes.

Examples:

- a user message naturally has a submission timestamp
- a tool call naturally has start and completion boundaries
- an assistant response may have start, first-output, and completion boundaries
- a generic transcript event does not always have a meaningful lifecycle pair

Therefore:

- `events` should remain a simple archive with one primary event timestamp
- richer lifecycle timing should live on turns or specialized event types

## SQLite / Storage Policy

SQLite is not the reason to avoid the wrong model here.

The main storage rule is:

- store durable product facts
- avoid making every noisy progress tick a forever fact

Expected posture:

- `sessions`: durable, long-lived
- `events`: durable archive
- `session_turns`: durable product timing truth
- `session_runtime_state`: latest-state overlay
- optional runtime-observation history: retained only if it proves operationally useful

If Longhouse later needs heavy multi-writer concurrency or large retained runtime history, that is an independent storage-engine discussion. The semantic model should still be the same.

## Provider Mapping

### Managed Producer Scope

The data model should be provider-agnostic, but launch-exact producers are narrower than that.

Launch scope for exact rows:

- the current managed-local path where Longhouse owns prompt dispatch
- the current runtime/presence substrate that can observe active and terminal phases
- the current transcript ingest path that can establish durable linkage

Claude hook transport is the reference producer today. Codex should use the same table only when it emits equivalent timing signals; until then the spec should not claim exact parity it cannot yet write.

Launch-scoped managed sessions should populate `session_turns` directly from real transport/runtime signals.

Canonical expectations:

- turn row created when the prompt is accepted
- `user_submitted_at` stamped immediately
- `send_accepted_at` stamped on verified transport acceptance
- `active_phase_observed_at` stamped only on a real observed active runtime phase; transport acceptance never substitutes for it
- `terminal_at` stamped on terminal phase
- transcript linkage stamped when durable user/assistant events are matched
- `timing_confidence = exact` only when every populated field on the row is backed by real managed/runtime/transcript signals, not just durable prompt persistence

The existing `managed_local_turns` ledger already proves most of this shape. The goal is to promote that concept into the real canonical model instead of keeping it as a managed-local-only sidecar.

### Imported / unmanaged sessions

Imported sessions usually only have transcript timestamps plus maybe partial runtime context.

For those sessions:

- `user_submitted_at` may come from the user event timestamp
- `durable_assistant_event_id` may come from the final assistant event timestamp linkage
- `send_accepted_at`, `active_phase_observed_at`, and `terminal_at` may be absent
- `timing_confidence` should be `partial` or `inferred`

Longhouse must not pretend imported sessions have exact execution timing when they do not.

## Continuations And Branching

Turns belong to concrete sessions, not abstract thread roots.

Rules:

- `session_turns.session_id` points at one concrete session
- launch ordering within a concrete session comes from `user_submitted_at`, `created_at`, and `id`
- thread-level analysis may aggregate turns across session continuations later
- turn linkage across continuations can be derived from the existing session lineage model instead of duplicating thread graph edges into the turn table on day one
- if `ordinal` is added later, it must be derived without making launch writes race-prone

## Machine Surface

This capability matters to agents and automation, so it belongs on `/api/agents/*`.

Minimum route family:

- `GET /api/agents/sessions/{session_id}/turns`
- `GET /api/agents/sessions/{session_id}/turns/{turn_id}`

Identifier contract:

- `session_id` remains the session UUID
- `turn_id` is the integer `session_turns.id`, not a UUID

Optional later routes:

- `GET /api/agents/sessions/{session_id}/turns/{turn_id}/events`
- `GET /api/agents/sessions/{session_id}/turns/{turn_id}/runtime`

Browser surfaces may mirror or wrap these, but they should not own the contract.

These routes must follow the active machine-surface canon:

- JSON-only
- UUIDs serialized as strings
- timestamps serialized as ISO-8601 UTC strings
- list response envelope `{turns, total}`
- current `X-Agents-Token` auth rules
- current `X-Longhouse-Session-Id` session-context rules where the caller is acting as a concrete session

Launch-minimum list contract:

- query params: `limit`, `offset`, `order`
- default `limit = 50`
- hard cap `limit = 100`
- `order = asc | desc`
- stable sort key:
  - `asc`: `user_submitted_at`, `created_at`, `id`
  - `desc`: reverse of the same tuple

Launch-minimum detail contract:

- response envelope `{turn}`
- exposes only canonical public fields from `session_turns`
- does not expose provider-private transport payloads

## Browser Implications

This spec does **not** require a millisecond browser counter.

The browser should consume canonical timestamps and decide how often to repaint. The durable model should stay correct even if the browser updates at 1 Hz, pauses in a background tab, or never opens at all.

For launch-quality UI:

- session elapsed can be rendered from session timestamps
- turn elapsed can be rendered from `session_turns`
- browser refresh cadence is a presentation concern, not a data-model concern

## Migration Plan

### Phase 1

- add `session_turns`
- add read/write service helpers
- define launch-minimum invariants and nullability explicitly
- define the hard-fail insertion/update boundary for canonical launch-scoped writes
- keep `managed_local_turns` in place temporarily
- make startup migration coverage explicit for the new table plus its partial unique index; do not assume `_migrate_agents_columns()` alone covers that rollout

### Phase 2

- make the launch-scoped canonical write path transactional or explicitly hard-failing
- dual-write the current managed-local producer path into `session_turns`
- normalize sentinel observation cursor values such as `0` to `NULL` before writing canonical linkage fields
- verify parity with `managed_local_turns`

### Phase 3

- add machine routes for turns
- migrate browser/session-control surfaces to read from `session_turns`

### Phase 4

- optional: reconstruct best-effort turn rows for imported sessions where it materially helps search/detail UX
- mark all such rows with explicit `timing_confidence`

### Phase 5

- do **not** remove `managed_local_turns` until `session_turns` also owns hydration/fallback behavior and ingest-driven durability reconciliation

## Non-Goals

- making every runtime heartbeat durable forever
- claiming exact timing for imported sessions that only provide transcript timestamps
- making the browser timer the source of truth
- turning `events` into a generic workflow-state table
- introducing Postgres-only assumptions into Longhouse core

## Success Criteria

- every launch-scoped managed prompt creates one canonical turn row, or the request fails noisily instead of leaving a silent hole
- the launch-minimum schema is backed by concrete producer rules, not aspirational fields
- the current managed-local producer path populates the same turn model end-to-end
- the data model can accommodate Claude/Codex parity without claiming exact writes before equivalent signals exist
- imported sessions can represent partial timing without pretending it is exact
- Longhouse can answer, for a launch-scoped managed turn: submit, send accepted, active observed, terminal, and durable times
- browser and agent clients can consume timing from `/api/agents/*` without provider-specific branching
- raw transcript archive remains simple and lossless

## Open Questions

- Should `first_output_at` be populated only when we have a real provider signal, or may it be reconstructed from the first durable assistant event?
- Do we want a separate `send_accepted_at` vs `user_submitted_at` distinction in launch UI, or is that only operator/debug detail?
- Should `assistant_completed_at` be distinct from `terminal_at` for all providers, or only where the provider/runtime contract makes that distinction real?
- For launch-scoped managed turns, when does a timeout become canonically `failed` instead of a transient condition that may still later reach `durable`?
