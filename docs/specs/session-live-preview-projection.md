# Session Live Preview Projection

Status: Draft

## Problem

Longhouse already separates durable transcript truth from runtime evidence:

- `AgentEvent` / `AgentSourceLine` are archive truth.
- `SessionObservation` is raw runtime and ingest evidence.
- `session_runtime_state` is the compact lifecycle read model.

The remaining leak is live transcript preview. Timeline/session-list views can still depend on raw `SessionObservation` payloads for `codex_bridge_live` preview text. That is wrong for a hot UI surface. The 2026-05-27 dogfood incident proved the failure mode: one active session had thousands of cumulative live-preview snapshots totaling hundreds of MiB of JSON, and a timeline request could load and parse that evidence history just to choose the latest card preview.

This is not a "SQLite cannot handle several agents" problem. It is a read-model boundary problem: a hot card view was reconstructing current UI state from an evidence log.

## Goal

Make live transcript preview a compact projection:

```text
runtime/bridge observation -> raw SessionObservation evidence
runtime/bridge observation -> upsert compact live-preview projection
timeline/session-list/session workspace -> read projection, never raw observation payloads
durable transcript ingest -> supersede projection when archive catches up
```

Durable history may be large. Hot UI state must stay tiny.

## Non-Goals

- Do not redesign durable transcript ingest.
- Do not remove `SessionObservation`; it remains useful for evidence, reducer replay, debugging, and ops.
- Do not introduce external infrastructure. SQLite remains the core store.
- Do not build a general event-sourcing framework.
- Do not make preview text searchable archive content.

## Data Model

Add a compact projection table, tentatively `session_live_previews`.

Fields:

- `session_id` primary key, foreign key to `sessions.id`
- `thread_id` nullable, for the current preview thread when available
- `turn_key` stable preview turn identity
- `seq` nullable integer, provider/bridge sequence
- `preview_text` text, current user-facing preview
- `provisional_cursor` text, matching the existing `TranscriptPreview` cursor format
- `provisional_complete` boolean, true when the bridge reports the turn complete
- `event_origin` text, default `live_provisional`
- `preview_observed_at` datetime
- `preview_updated_at` datetime
- `source` text, for example `codex_bridge_live`
- `last_observation_id` text, idempotency and forensic anchor back to `SessionObservation.observation_id`
- `superseded_at` nullable datetime
- `superseded_by_event_id` nullable integer
- `superseded_reason` nullable text, using existing stale reasons such as `superseded_by_durable`

Indexes:

- primary key on `session_id`
- `preview_updated_at` for ops/debug sorting
- `last_observation_id` for forensic lookup

Cardinality contract:

- At most one active preview row per session.
- The projection stores the latest visible preview for the session, not historical snapshots.
- Historical preview evidence remains in `SessionObservation`, bounded by retention, but ordinary UI does not read it.

## Write Rules

### Runtime Preview Ingest

When `ingest_runtime_events()` receives a bridge transcript delta:

1. Persist the raw observation in `SessionObservation`.
2. Parse the payload once while it is already in memory.
3. Upsert `session_live_previews` for `session_id`.
4. If `last_observation_id` already equals the incoming observation id, no-op.
5. Only replace the row when the incoming candidate is newer:
   - if `incoming.turn_key != existing.turn_key`, replace when `incoming.preview_observed_at >= existing.preview_observed_at`; turn changes reset `seq`
   - within the same turn, higher `seq` wins
   - if either side lacks `seq`, use `preview_observed_at` as the primary ordering key
6. Publish the same session runtime update used today.

The projection update must be in the same `WriteSerializer` transaction as the runtime observation write. No second poller is required for the normal path.

### Durable Transcript Catch-Up

When durable ingest inserts archive events for a session:

1. Update `sessions.last_activity_at` as today.
2. If the durable activity timestamp is at or after `session_live_previews.preview_observed_at`, update the row with:
   - `superseded_at`
   - `superseded_by_event_id`
   - `superseded_reason='superseded_by_durable'`
3. Readers ignore superseded rows for active preview display, while the row remains available for short-term forensics.

Do not delete the projection row on the hot path. One row per session is bounded, and update-in-place avoids flicker and keeps the catch-up transition debuggable.

### Observation Rebuild / Startup Repair

Rebuild is a recovery path, not a hot path.

Provide a bounded rebuild helper:

- input: session ids or recent active sessions
- reads at most `MAX_ACTIVE_PREVIEW_OBSERVATIONS_PER_SESSION = 50` recent bridge preview observations per session
- uses the existing `(session_id, source, kind, observed_at, id)` index shape
- recreates projection rows

Do not run an unbounded full-table rebuild during startup.

## Read Rules

Timeline/session-list/session workspace APIs must not read raw preview observation payloads.

Allowed:

- join/load `session_live_previews`
- read `session_runtime_state`
- read session counts, summary, title, last activity
- page durable transcript events in session detail endpoints

Forbidden in hot read paths:

- querying `SessionObservation.payload_json` for preview text
- parsing bridge preview payloads
- aggregating preview history from `SessionObservation`
- using `AgentEvent` live/provisional rows as preview source

This invariant should be enforced with tests, not just convention.

Freshness remains read-side:

- readers compose preview freshness from `SessionRuntimeState.freshness_expires_at`
- expired runtime freshness surfaces as `is_stale=true` / `stale_reason='freshness_window_expired'`
- no timer is required to mutate projection rows from fresh to stale

Session-less runtime states cannot have preview rows. All projection joins are left joins by `session_id`, and readers must tolerate a missing projection.

## API Contract

No wire change is required for the first implementation.

Existing response fields that expose `TranscriptPreview` or equivalent should keep the same shape, but their source changes:

- previous: derived from latest `SessionObservation` rows
- new: read from `session_live_previews`

If a future API field exposes projection diagnostics, keep it under a debug/admin shape, not public client contract.

Projection columns intentionally mirror existing response vocabulary:

- `preview_text` -> response text
- `event_origin` -> existing `live_provisional` origin
- `preview_observed_at` -> response timestamp
- `provisional_cursor` -> response `content_cursor`
- `provisional_complete` -> response `is_complete`
- read-side freshness/supersession -> response `is_stale` and `stale_reason`

## Retention

`session_live_previews` is small and can retain one row per session until superseded or session archival cleanup.

`SessionObservation` retention remains separate:

- Keep recent debug evidence for live preview observations.
- Delete rows already covered by durable archive activity.
- Keep bounded per-session windows for active sessions.

Retention should reduce DB growth, but correctness must not depend on retention running. The hot UI must stay safe even if observation cleanup is disabled or behind.

First deploy behavior: existing active sessions may not have projection rows until their next live preview delta or a bounded repair run. In pre-launch dogfood this is acceptable; the fallback must be "no preview", not "scan observations".

## Testing Contract

Unit tests:

- bridge transcript observation upserts one projection row
- newer preview replaces older preview
- older/out-of-order preview does not replace newer preview
- new turn with reset `seq` replaces old turn when observed at or after the existing preview
- duplicate observation does not duplicate projection state
- durable ingest supersedes projection
- projection survives deletion of the raw observation row
- preview text is not inserted into `AgentEvent`
- search/count/session event APIs ignore preview text
- rebuild helper reads only bounded recent observations

Integration tests:

- timeline session cards show preview from projection
- timeline session card loading does not call `load_active_provisional_preview_map`
- session workspace stream publishes updates after projection changes
- iOS/mobile tail/session endpoints preserve existing response semantics
- a session with thousands of raw preview observations still causes at most one projection-row read per visible session
- OpenAPI schema for `SessionTranscriptPreviewResponse` does not drift
- iOS decoding accepts fixtures covering fresh, freshness-expired, missing-timestamp, and superseded preview responses

Guard tests:

- hot timeline/session-list code must not query `SessionObservation.payload_json` for preview text
- raw observation payload parsing is allowed only in ingest/rebuild/debug paths

Performance tests:

- seed many bridge preview observations for one session
- verify timeline card endpoint completes under 250ms in the focused SQLite test with 10k preview observations and 100 visible sessions
- verify read count/parsed payload count remains bounded

## Implementation Phases

### Phase 0: Spec And Review

- Commit this spec.
- Send it to Hatch Opus for first-principles review.
- Incorporate concrete review changes before implementation.

### Phase 1: Schema And Projection Service

- Add `SessionLivePreview` model/table.
- Add auto-additive migration coverage.
- Implement projection helpers:
  - `preview_candidate_from_runtime_event`
  - `upsert_session_live_preview`
  - `supersede_session_live_preview`
  - `load_session_live_preview_map`
  - bounded rebuild helper
- Unit test ordering, duplication, and supersession.

### Phase 2: Runtime And Durable Ingest Integration

- Wire bridge preview runtime ingest to upsert projection.
- Wire durable transcript ingest to clear/supersede projection.
- Wire pubsub/workspace preview publishing to the projection or pass the upserted projection candidate directly; it must not re-query observations.
- Keep observation retention as cleanup, not correctness.
- Add integration tests around runtime ingest and durable catch-up.

Review gate: Hatch Opus review after this phase.

### Phase 3: Hot Read Migration

- Replace `load_active_provisional_preview_map()` usage in timeline/session-list/session workspace hot paths with `load_session_live_preview_map()`.
- Keep the old observation loader only for rebuild/debug tests or remove it if no longer needed.
- Put the read swap behind a temporary kill switch for the first deploy: projection reads are on by default, and `LONGHOUSE_DISABLE_LIVE_PREVIEW_PROJECTION=1` can force the legacy path during dogfood burn-in.
- Add guard tests proving hot paths do not parse raw observation payloads.
- Verify web/iOS response shapes remain stable.

Review gate: Hatch Opus review after this phase if the read swap or API shape changes materially.

### Phase 4: Broad Verification And Cleanup

- Focused server tests:
  - `tests_lite/test_provisional_transcript_events.py`
  - `tests_lite/test_session_runtime.py`
  - timeline/session workspace tests touched by the change
- Broader backend test tier if the patch touches shared session projection.
- Final Hatch Opus review.
- Commit each coherent phase separately.

## Success Criteria

- Timeline/session-list hot reads do not depend on raw `SessionObservation` preview payloads.
- Live previews still appear quickly for active sessions.
- Durable transcript remains authoritative for archive/search/export.
- Preview projection remains bounded to one row per session.
- Observation retention can lag or fail without making timeline slow.
- Tests encode the boundary strongly enough that future preview/provider work cannot accidentally regress to scanning evidence logs.
