# Session Event Identity Plan

## Purpose

Longhouse should present managed sessions as one coherent stream of work while
preserving two different delivery guarantees:

- **Live UI guarantee:** current runtime state and visible output should reach
  timeline cards in tens to hundreds of milliseconds under nominal conditions.
- **Archive guarantee:** durable transcript history must be correct, ordered,
  replayable, retryable, searchable, and source-line preserving.

The target is not one slow path. The target is one understandable event identity
model where live delivery and archive delivery are materializations of the same
session facts wherever the provider gives us enough identity to prove that.

## Current State

Longhouse currently has three relevant mechanisms:

1. **Bridge live overlay**
   - Producer: managed Codex bridge/app-server notifications.
   - Server path: `/api/agents/runtime/events/batch`.
   - Storage: `session_runtime_events` rows with `source="codex_bridge_live"`.
   - Identity today: `source + dedupe_key`, where the dedupe key is scoped to
     session/thread/turn/sequence.
   - Limitation: bridge live rows do not carry transcript `source_path`,
     `source_offset`, raw source hash, or provider event UUID, so they are
     provisional UI observations, not durable transcript events.

2. **Live file ingest**
   - Producer: Machine Agent live work (`WorkPriority::Live`) reading provider
     rollout/transcript files.
   - Server path: `/api/agents/ingest`.
   - Storage: `events` rows through `SourceLineMode::EventOnly`.
   - Identity today: same file-derived identity as archive ingest for event rows:
     `session_id`, `source_path`, `source_offset`, and event hash/provider UUID
     where available.
   - Limitation: omits full source-line archive rows for latency, so the archive
     cursor still has to catch up.

3. **Archive ingest**
   - Producer: Machine Agent full file shipper.
   - Server path: `/api/agents/ingest`.
   - Storage: `events` and `source_lines`.
   - Identity today: durable transcript identity and replay/source-line truth.

## First Principles

- The durable transcript is canonical for detail, search, replay, export, and
  memory.
- The live bridge overlay is canonical only for temporary timeline-card preview.
- File-derived live ingest and archive ingest should converge on the same
  durable `AgentEvent` identity.
- Runtime state should answer lifecycle/control-path questions; it should not be
  a second transcript archive.
- Clients should render server-projected runtime/card facts instead of deriving
  a parallel state machine from raw fields.

## Goals

1. Make live transcript overlays explicit as provisional timeline-card preview.
2. Prove that file-derived live and archive ingest share event identity.
3. Measure divergence between bridge overlay, live file ingest, and full archive
   ingest before removing or merging any pipeline pieces.
4. Move unreduced bridge overlay text out of generic session/detail contracts.
5. Reduce duplicate display-state derivation in the frontend once server card
   projection is trusted.

## Non-Goals

- Do not collapse live UI delivery into durable archive ingest.
- Do not insert bridge overlay text into `AgentEvent` unless it can be matched to
  stable provider/source identity.
- Do not merge `file_state` and `live_file_state` until measurements show the
  cursor split is causing real misses. Their failure semantics are intentionally
  different today.
- Do not introduce new event identity columns until tests prove the existing
  file-derived identity is insufficient.

## Success Criteria

### Instrumentation

- For managed Codex turns, we can report:
  - bridge live overlay events accepted/skipped,
  - live file events inserted/skipped,
  - archive events inserted/skipped,
  - source lines inserted,
  - overlay superseded/orphaned counts,
  - latency from bridge observation to visible card preview,
  - latency from provider file append to durable `AgentEvent`.

### Identity

- A test proves that EventOnly and Full ingest for the same rollout source line
  produce the same event identity tuple:
  `session_id`, `source_path`, `source_offset`, `event_hash`, and provider
  `event_uuid` when present.
- Replaying archive ingest after EventOnly ingest does not duplicate events and
  does backfill source lines.

### UI Contract

- Timeline cards may show provisional live preview.
- Detail, search, replay, export, and memory use durable transcript events.
- Generic session/detail responses do not imply that bridge overlay text is
  transcript truth.
- If an overlay is older than durable transcript activity, the overlay is hidden
  or marked superseded.

### Operational Safety

- Existing hosted and iOS clients tolerate the transition through additive fields
  or one release of deprecation.
- No launch-critical behavior depends on a browser-only contract.
- Shipping preserves the live-lane latency budget and archive correctness.

## Implementation Plan

### Phase 1: Make Current Truth Honest

- Document this plan.
- Add focused tests proving file-derived identity convergence and source-line
  backfill after EventOnly-style ingest.
- Keep live transcript overlays provisional in naming and API descriptions.
- Stop expanding `live_transcript` into additional generic surfaces.

### Phase 2: Measure Divergence

- Add managed Codex counters and/or profiler output for bridge overlay, live file
  ingest, archive ingest, source-line catch-up, superseded overlays, and orphaned
  overlays.
- Include bridge restart mid-turn as a distinct failure bucket because bridge
  sequence is process-local.

### Phase 3: Carry Better Provisional Identity

- Preserve Codex `item.id` from notifications where available.
- Carry item identity in bridge overlay payloads as provisional item identity.
- Use measurements to decide whether provisional bridge overlay can be matched to
  eventual file-derived assistant events reliably.

### Phase 4: Narrow Projections

- Move bridge overlay presentation toward timeline-card-only response fields.
- Keep old optional fields temporarily for generated clients and iOS.
- Collapse frontend runtime derivation onto server `timeline_card` and
  `runtime_facts` once compatibility is safe.

### Phase 5: Storage Cleanup

- If measurements justify it, move latest bridge overlay text out of unreduced
  `session_runtime_events` rows and into the smallest existing runtime state
  surface that can answer timeline-card queries.
- Revisit cursor unification only after the measured behavior proves live and
  archive cursors no longer need different failure semantics.

## Decision Rules

- Prefer proving existing identity over adding new identity columns.
- Prefer a derived view over a persisted status enum until repeated query cost or
  correctness requires persistence.
- Prefer one release of additive compatibility over breaking generated clients.
- Delete or narrow projection logic before adding new runtime tables.
