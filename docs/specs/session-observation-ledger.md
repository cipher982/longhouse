# Session Observation Ledger

Status: Draft for implementation on `session-observation-ledger`
Owner: Longhouse session kernel
Updated: 2026-05-12

## Problem

Longhouse now has an honest transcript preview contract, but the storage model
still reflects the path it grew through:

- `source_lines` stores durable provider archive rows.
- `events` stores normalized transcript rows and active provisional bridge text.
- `session_runtime_events` stores runtime/liveness signals.
- the Machine Agent has a local phase ledger that is later reflected into hosted
  runtime state.

Those tables are individually reasonable, but as a system they make "where did
this pixel come from?" harder than it should be. A transcript card, session
detail row, runtime badge, and export can be derived from different raw logs and
different reducer entrypoints.

Pre-launch, the cleanest end state is not to preserve each historical table as a
first-class ledger. The clean end state is one append-only observation log, then
small deterministic reducers.

## Goal

Introduce `SessionObservation` as the only raw session observation bus.

Every session-related fact that arrives from a machine or provider enters this
table first:

- provider transcript archive lines
- provider-normalized transcript events when no raw source line exists
- bridge transcript deltas
- runtime phase/progress/terminal/binding signals
- control-path and managed-session signals

Reducers own every read model:

- transcript events for detail/tail/search display
- source archive/export rows
- runtime state for timeline/liveness
- timeline card projections
- search, summary, embedding, and memory inputs

The UI and APIs should never read provider-specific write-side ledgers directly.

## First Principles

- Raw observations are immutable facts about what Longhouse received.
- Read models are disposable. If a read model cannot be rebuilt from
  observations, it is carrying hidden truth.
- Runtime and transcript are different domains, but they do not need different
  raw ledgers.
- Provisional bridge text is an observation, not durable transcript truth.
- Durable provider archive rows are observations too; their authority comes from
  reducer rules, not from living in a special table.
- Idempotency belongs at observation identity first, then reducer identity.

## Proposed Model

`session_observations` is append-only except for operational compression or
backfill migrations.

| Field | Meaning |
| --- | --- |
| `id` | Local autoincrement row id. |
| `observation_id` | Deterministic unique id for idempotency. |
| `session_id` | Longhouse session id when known. |
| `runtime_key` | Runtime binding key when the observation is not yet tied to a session. |
| `provider` | Provider such as `codex`, `claude`, or `gemini`. |
| `device_id` | Machine identity when supplied. |
| `source_domain` | Broad domain: `transcript`, `runtime`, `control`, `engine`. |
| `source` | Producer/source label, for example `agents_ingest` or `codex_bridge_live`. |
| `kind` | Semantic kind, for example `provider_source_line`, `provider_event`, `runtime_signal`, `bridge_transcript_delta`. |
| `source_path` | Provider file path when applicable. |
| `source_offset` | Provider byte offset when applicable. |
| `source_cursor` | Monotonic cursor inside the producer stream. |
| `observed_at` | Time the producer says the observation happened. |
| `received_at` | Runtime Host receive time. |
| `payload_json` | Lossless JSON payload for the observation. |
| `payload_json_z` / `payload_json_codec` | Optional compressed payload storage. |

Observation identity examples:

- source archive line:
  `source_line:{sha256(session_id, branch_id, source_path, source_offset, line_hash)}`
- bridge transcript delta:
  `runtime:{source}:{dedupe_key}`
- runtime phase/progress/terminal/binding signal:
  `runtime:{source}:{dedupe_key}`
- provider event without source line:
  `provider_event:{sha256(session_id, branch_id, event_uuid or source identity or event hash)}`

## Target Flow

### Managed Codex Bridge Delta

1. Engine emits `RuntimeEventIngest(source=codex_bridge_live, progress_kind=bridge_live_transcript_delta)`.
2. Runtime Host writes one `SessionObservation(kind=bridge_transcript_delta)`.
3. Runtime reducer may write/update runtime state when the event is a runtime
   signal.
4. Transcript reducer upserts one active provisional transcript read row keyed by
   session/thread/turn.
5. Timeline and detail read the same transcript reducer output.

### Durable Provider Archive

1. Machine Agent ships provider source lines and parsed events.
2. Runtime Host writes `SessionObservation(kind=provider_source_line)` for each
   source line and `provider_event` only when no source line exists.
3. Transcript reducer derives durable transcript rows.
4. Reconciliation links or supersedes active provisional rows from bridge
   observations.
5. Export uses the source archive reducer output, not the old write-side table.

### Runtime State

1. Presence, bridge, process, and terminal observations enter
   `SessionObservation(kind=runtime_signal)`.
2. Runtime reducer materializes `session_runtime_state`.
3. Timeline badges read runtime state only.

## Current Table Fate

During implementation, existing tables can remain as reducer outputs while the
observation path is introduced. They should lose "ledger" status:

- `source_lines` becomes `SourceArchive`, a rebuildable export/read model.
- `events` becomes `TranscriptEvent`, a rebuildable transcript read model.
- `session_runtime_events` is deleted or demoted once `SessionObservation`
  carries all runtime inputs.
- `session_runtime_state` remains a materialized runtime read model.

No new public compatibility field should be added for old `live_transcript` or
overlay semantics.

## Implementation Phases

### Phase 1: Observation Bus

- Add `SessionObservation`.
- Add an idempotent writer service.
- Record observations for runtime events, bridge transcript deltas, and source
  archive lines.
- Keep current reducers writing existing read models.
- Add tests proving one managed Codex bridge delta and one durable archive line
  land in the same observation table and still reconcile in the current read
  model.

### Phase 2: Reducer Boundary

- Move provisional transcript materialization behind a transcript reducer module
  that accepts observations rather than `RuntimeEventIngest`.
- Move durable transcript insertion behind the same reducer boundary.
- Make rebuild from observations a testable command/service.

### Phase 3: Runtime Event Collapse

- Replace `session_runtime_events` writes with `SessionObservation` runtime
  observations.
- Rebuild `session_runtime_state` directly from observations.
- Remove direct runtime-event reads from timeline stream freshness signatures, or
  point them at observation cursors.

### Phase 4: Source Archive Collapse

- Rebuild source archive/export rows from `provider_source_line` observations.
- Remove direct source-line write authority from ingest.
- Keep source archive as a derived read model only if export performance needs it.

### Phase 5: Cleanup

- Rename code and docs so only `SessionObservation` is called a ledger.
- Delete dead compatibility paths and stale tests.
- Add a cold rebuild test that drops read models, replays observations, and gets
  the same transcript/runtime/timeline projections.

## Success Criteria

- A managed Codex bridge delta creates exactly one observation and one active
  provisional transcript read row for the turn.
- A newer bridge snapshot updates the same provisional transcript read row
  without appending a duplicate visible message.
- Durable archive observations reconcile or supersede provisional transcript rows.
- Timeline card and session detail read the same transcript reducer output.
- Search, export, summaries, embeddings, and memory never consume provisional
  observations as durable truth.
- Runtime state can be rebuilt from runtime observations alone.
- Source export can be rebuilt from source observations alone.
- A cold rebuild from `SessionObservation` recreates transcript rows, runtime
  state, and timeline projections.
