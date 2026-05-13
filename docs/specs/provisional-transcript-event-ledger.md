# Provisional Transcript Event Ledger

Status: Superseded by `session-observation-ledger.md`
Owner: Longhouse session kernel
Updated: 2026-05-12

This note describes the provisional transcript projection that preceded
`SessionObservation`. The current write-side authority is
`session_observations`; bridge transcript deltas enter as observations and then
materialize into provisional `AgentEvent` rows.

## Problem

Managed Codex used to have two transcript-looking surfaces:

- bridge text in runtime signals, projected through a runtime overlay
- source-backed transcript rows in `events` / `source_lines`

That made the timeline card faster, but it also made the data model harder to
reason about. A timeline card could show an older "Live output" snippet while
session detail already had newer durable transcript rows. Freshness metadata
made the UI honest, but it did not remove the redundancy.

## Goal

Make the event ledger the only transcript-shaped read source.

The live bridge lane may still be low-latency, and the archive lane remains the
canonical source of truth, but both should materialize through `AgentEvent`
identity:

- bridge live text creates or updates an explicitly provisional event row
- source-backed ingest creates durable event rows from provider transcript files
- reconciliation marks provisional rows as replaced when durable truth catches up
- timeline/detail clients read one ordered event projection instead of a second
  provisional transcript preview projection

## First Principles

- Runtime events answer runtime questions: phase, tool, liveness, terminal state,
  freshness, and control-path health.
- Transcript events answer transcript questions: what text/tool activity should
  be shown to the user.
- Source-backed events are durable truth.
- Bridge-backed events are observations. They are useful only while active and
  must never leak into search, export, summaries, embeddings, or replay.
- Reconciliation must be idempotent. Replaying archive ingest or re-sending a
  live bridge snapshot cannot create duplicate visible transcript rows.

## Proposed Model

Extend `AgentEvent` with explicit provisional metadata:

| Field | Meaning |
| --- | --- |
| `event_origin` | `durable` for source-backed/history events, `live_provisional` for bridge live text. |
| `provisional_state` | `active`, `reconciled`, or `superseded` for provisional events; `null` for durable rows. |
| `provisional_key` | Stable per live stream/turn key, e.g. `codex_bridge_live:<session>:<thread>:<turn>`. |
| `provisional_cursor` | Monotonic snapshot cursor including sequence, e.g. `...:<seq>`. |
| `provisional_seq` | Latest bridge sequence applied to the row. |
| `provisional_complete` | Whether the bridge reported turn completion for this snapshot. |
| `reconciled_event_id` | Durable `AgentEvent.id` that replaced this provisional event. |

Durable rows keep the existing source identity:

- `session_id`
- `branch_id`
- `source_path`
- `source_offset`
- `event_hash`
- provider-native `event_uuid` where available

## Evidence Baseline

Existing backend tests already prove that file-derived live ingest and archive
ingest converge when they share `source_path`, `source_offset`, and event hash:
`server/tests_lite/test_session_event_identity.py`.

The managed Codex profiler artifact from
`artifacts/managed-session-propagation/managed-phase2-20260507181856` shows the
important bridge/archive mismatch:

- bridge send returned `thread_id=019e04bc-f9ff-7d02-a5ff-4eb292419eaf` and
  `turn_id=019e04bd-03ee-73b1-950d-0d0ee89d1c44`
- local rollout contained the final assistant text in both
  `event_msg.agent_message` and `response_item.message`
- the rollout `task_complete` row carried the same `turn_id`
- the source-backed assistant `response_item` did not expose a stable item id in
  the archived row from that run

That means the first reconciliation pass should not depend on a perfect provider
item id. It should use the stable provisional turn key for upserts, then match
or supersede provisional rows against durable assistant text once the archive
arrives.

## Lifecycle

### Live Bridge Snapshot

When the Runtime Host accepts a `codex_bridge_live` event with
`progress_kind=bridge_live_transcript_delta`:

1. Store the runtime event for runtime diagnostics and existing stream wakeups.
2. Upsert one `AgentEvent` with `event_origin=live_provisional`.
3. Use a stable `provisional_key` scoped to session/thread/turn, not the sequence.
4. Ignore older or duplicate sequence snapshots.
5. Update the row text, timestamp, cursor, seq, and complete flag for newer
   snapshots.

### Durable Archive Catch-Up

When source-backed ingest inserts durable events:

1. Insert/archive source-backed rows exactly as before.
2. Find active provisional assistant rows for the same session.
3. Match a provisional row to a durable assistant text row when the durable text
   equals or clearly extends the provisional text within the same recent turn
   window.
4. Mark matched provisional rows `reconciled` and set `reconciled_event_id`.
5. Mark unmatched older provisional rows `superseded` when newer durable
   transcript activity exists.

### Projection

Default transcript projections include:

- all durable rows in the requested branch/context
- active provisional rows that have not been reconciled or superseded

Default transcript projections exclude:

- reconciled provisional rows
- superseded provisional rows
- provisional rows from search, export, embeddings, summaries, and memory

Timeline cards may label an active provisional row as live output, but the label
comes from event metadata, not a separate overlay object.

## Task List

### Phase 1: Evidence and Spec

- Validate current bridge payload shape and durable rollout event shape from
  tests/artifacts.
- Add or update focused tests showing the current split:
  bridge live snapshot, durable ingest catch-up, projection behavior.
- Land this spec with concrete success criteria.
- Opus review gate: ask whether the model is simpler than the overlay approach
  and whether any state is unjustified.

### Phase 2: Backend Ledger

- Add `AgentEvent` provisional metadata columns and SQLite migration.
- Materialize live bridge snapshots into active provisional `AgentEvent` rows.
- Add idempotent update rules for duplicate/older bridge snapshots.
- Reconcile provisional rows after durable ingest.
- Filter inactive provisional rows out of default event queries.
- Exclude provisional rows from summaries, embeddings, export, and search.
- Commit in small backend steps with tests after each.

### Phase 3: API/UI Projection

- Add event metadata fields to event responses.
- Add `transcript_preview` to session projections. It is sourced from the
  latest active provisional ledger event, carries `is_provisional`, `is_stale`,
  and cursor metadata, and is the only field timeline cards render for bridge
  text.
- Remove the previous runtime-overlay response field so clients have one
  transcript preview contract.
- Update frontend tests and fixture captures so cards render only
  `transcript_preview`.
- Run UI capture for timeline card stress scenes.

### Phase 4: Final QA and Ship

- Run targeted backend tests for ingest/runtime/reconciliation.
- Run frontend tests for session timeline/card rendering.
- Run E2E or fixture-backed UI QA proving:
  - active live text appears quickly,
  - durable catch-up removes the provisional duplicate,
  - older bridge output cannot replace newer durable output.
- Opus final review.
- Push/ship according to the normal Longhouse lane.

## Success Criteria

- A bridge live snapshot creates exactly one active provisional event for a turn.
- A newer live snapshot updates that same event instead of appending another row.
- Durable archive ingest reconciles matching provisional rows and does not create
  duplicate visible transcript messages.
- Durable archive ingest supersedes stale unmatched provisional rows.
- Timeline cards read the same event-ledger-backed preview projection that
  detail/tail can reconcile against. The previous runtime overlay is no longer
  part of the API.
- Search, export, summaries, embeddings, and replay never consume provisional
  rows as durable truth.
- Existing runtime/liveness behavior remains intact.

## Non-Goals

- Do not collapse live and archive shipping cursors. Their retry semantics are
  still different.
- Do not make provisional rows source-backed or durable by naming convention.
  They are observation-derived projection rows until reconciled.
