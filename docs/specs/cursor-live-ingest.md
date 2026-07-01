# Cursor Live Transcript Ingest — Spec

## Subject

Cursor sessions do not stream their transcript to the timeline live. This is
the one remaining Cursor parity gap after the Helm/Console/Shadow build.

## Current state (verified)

- The Rust engine has **no** `~/.cursor` tailer. Its only Cursor modules are
  `cursor_acp` (Console one-shot ACP), `cursor_helm_control` (Helm socket IPC),
  and `managed_cursor_helm_scan` (Helm state-file → heartbeat lease). The
  shipper's `CursorMode` is a file-offset cursor, unrelated to Cursor.
- Cursor transcripts land on the timeline only via the post-hoc
  `longhouse cursor import` CLI (decodes `~/.cursor/chats/<id>/store.db` through
  `zerg.services.cursor_transcript` and POSTs a `SessionIngest` to
  `/api/agents/ingest`) and via `local_health` discovery.
- Therefore:
  - **Shadow** sessions are not live-tailed; they appear on scan cadence, not
    as turns commit.
  - **Helm** sessions are steerable (send/interrupt/terminate work) but the
    managed session has **no transcript on the timeline** until a later
    `longhouse cursor import`, which creates a *separate unmanaged* session
    rather than binding events to the managed Helm session id.

## The seam decision (needs PM blessing)

Two flows, two natural homes — keep them split (no clever reuse):

### A. Helm live ingest → launcher-side Python tailer (recommended)

The `longhouse cursor` launcher already owns the managed session id and runs in
Python, where the `cursor_transcript` decoder already lives. Add a daemon
thread in `server/zerg/cli/cursor_helm.py` that:

1. After `pty.fork`, discovers this session's chat dir under `~/.cursor/chats/`
   (newest `store.db` created after launch, optionally matched by cwd from the
   decoded session; `LH_CURSOR_HELM_CHAT_DIR` env override for dogfood
   determinism).
2. Polls every few seconds, calls `decode_store_db(path)`, diffs against the
   last-shipped event set by `source_offset`, and POSTs only new `EventIngest`
   rows to `/api/agents/ingest` with `id = <managed session id>` so they bind
   to the Helm session.
3. Stops on the existing `stop_event`.

Pros: reuses the existing decoder; attributes events to the managed session;
no Rust port; engine restart does not interrupt ingest (launcher keeps
tailing). Cons: only helps Helm, not Shadow.

### B. Shadow live ingest → engine Rust tailer (defer)

Benefiting all unmanaged Cursor sessions requires the engine to watch
`~/.cursor/chats/` and tail each `store.db`-WAL. This needs a Rust port of the
protobuf blob-DAG decoder (the Python decoder in `cursor_transcript.py` is not
trivially portable) and session attribution by cwd/started-at. This is the
deferred `unmanaged_bindings.rs`-equivalent for Cursor and benefits all
unmanaged providers, not just Cursor. Defer until the Helm path proves the
decoder-attribution approach.

## Open questions to resolve before building A

1. **Ingest idempotency — RESOLVED, with a blocker.** `/api/agents/ingest` does
   dedupe (via `reduce_provider_event_observation` → `_find_existing_provider_event`
   keyed on `event_hash`/content, and a lossless `(source_path, source_offset)`
   path). BUT the cursor decoder in `cursor_transcript._burst_aware_timestamps`
   **synthesizes timestamps across the whole session** between `createdAt` and
   `updatedAt`. As the session grows (`updatedAt` advances), earlier events'
   synthesized timestamps shift, changing their `event_hash`, so a naive
   "re-decode + re-post every poll" tailer would insert **duplicates** on the
   timeline. This must be solved before the tailer ships. Two candidate fixes:
   - assign a stable `source_offset` per event in the decoder (e.g. the message
     index in `ordered_ids`) so ingest dedupes by `(source_path, source_offset)`
     independent of timestamp; or
   - have the tailer track already-shipped events by `raw_json` hash and only
     POST genuinely new events (shipped events keep their first-seen timestamps).
   The first is cleaner and also fixes `longhouse cursor import` re-run safety.
2. **Chat dir discovery heuristic**: is "newest `store.db` created after
   launch" reliable, or does Cursor reuse/rotate chat dirs? Needs a live
   dogfood observation; the `LH_CURSOR_HELM_CHAT_DIR` override de-risks the
   first run.
3. **WAL vs full decode**: `decode_store_db` opens the DB read-only and
   decodes the full DAG each call. Acceptable on a multi-second cadence for a
   single session, but if the DAG grows large we may need incremental
   decode keyed off `latestRootBlobId` changes.

## Recommendation

Ship A (Helm launcher-side tailer) once ingest idempotency is confirmed; keep
B on the roadmap. This matches "one session, one execution owner" — the
launcher owns the Helm session, so it owns that session's live ingest too.

## Status

**A is implemented** in `server/zerg/cli/cursor_helm_ingest.py`, wired into the
Helm launcher (`run_helm` spawns a `cursor-helm-ingest` daemon thread). It uses
the append-only high-water-mark approach: each poll decodes the current
`store.db`, stamps every event with a stable ordinal `source_offset`, ships
only events with `ordinal >= hwm`, and advances the HWM only after a
successful (<500) post. Each event is shipped exactly once, so synthesized
timestamps never shift and no duplicates arise. Chat-dir discovery honors
`LH_CURSOR_HELM_CHAT_DIR` for deterministic dogfood, else scans
`~/.cursor/chats/*/*/store.db` for the newest store created around launch.
Tests in `tests_lite/test_cursor_helm_ingest.py` cover the HWM/delta logic,
retry-on-failure, and discovery.

Open during first dogfood: confirm the discovery heuristic locks onto the
right chat dir for a real `cursor-agent` session, and confirm events appear on
the timeline bound to the managed session id. B (Shadow engine Rust tailer)
remains deferred.
