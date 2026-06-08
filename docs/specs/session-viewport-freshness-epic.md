# Session Viewport Freshness Epic

Status: Draft, revised after Hatch Opus review
Last updated: 2026-06-08

## Executive Summary

The iOS session detail view must never display a transcript that is known only
to a local cache while missing durable rows already present on the Runtime Host.

The motivating bug was a notification-driven iOS open that rendered a cached
tail ending before a large assistant message and a pause-request question. The
durable archive and `/mobile-tail` projection already contained the missing
rows; the client simply treated a warm cache as fresh enough and then attached
to an SSE stream with `skip_initial=true`, so the event that caused the
notification could be missed.

The immediate fix is correct: cache is instant paint only, and session open
always reconciles with the durable tail. This epic turns that fix into a
system invariant across iOS and web:

1. Cached transcript state is never freshness authority.
2. A session viewport is a durable snapshot plus a revision.
3. Realtime streams are subscribed against a known snapshot revision.
4. Process-local pubsub sequence is a latency optimization, not correctness.
5. Notification/deep-link opens force the same durable reconciliation path as a
   cold open.

The target architecture is "snapshot first, subscribe from snapshot." The
practical implementation should be additive and phased; do not replace the
archive/projection stack or invent a new transcript transport.

Hatch Opus review found one blocking defect in the current server primitive:
the workspace signature does not currently move when a pause-request question
appears. That is not an implementation detail; pause requests are viewport
state, and they were part of the motivating failure. Phase 2 must fix and test
that before any stream handshake relies on a fingerprint.

## Problem Statement

Longhouse currently has the right underlying pieces, but their ownership is
ambiguous:

- `/api/timeline/sessions/{id}/mobile-tail` provides durable truth for iOS.
- `/api/timeline/sessions/{id}/workspace` provides browser workspace bootstrap.
- `/api/timeline/sessions/{id}/workspace/stream` provides thin SSE invalidation.
- iOS keeps an in-memory transcript cache and a durable disk snapshot.
- iOS also persists a pubsub sequence for bounded stream replay.
- Polling exists as a fallback when the stream is down.

Each piece is reasonable in isolation. The bug came from the combined behavior:
a cache hit could suppress the durable REST refresh while the stream deliberately
skipped its initial workspace event.

The system needs one explicit correctness rule: a rendered session viewport is
fresh only after the Runtime Host has returned a durable snapshot, or after a
stream event has caused a durable snapshot refresh.

## Current Topology

### Server

- `build_session_workspace(...)`
  - builds browser workspace metadata, thread, and first projection page.
- `build_session_mobile_tail(...)`
  - builds compact iOS payload with focused session metadata, projected tail,
    and `snapshot_event_id` for older-page drift detection.
- `_load_workspace_signature(...)`
  - computes a cheap signature over session update time, latest durable event,
    runtime state, runtime version sum, thread count, latest event timestamp,
    and live preview update time.
  - currently does not cover pause-request rows and may not cover managed
    control state; this is a confirmed gap for this epic to close.
- `_session_workspace_stream(...)`
  - emits `workspace_changed` when the signature changes.
  - supports `Last-Event-ID` using process-local pubsub sequence replay.
  - supports `skip_initial=true`, which waits for the next publish before
    doing the first signature comparison.

### iOS

- `SessionViewModel.start(...)`
  - hydrates from memory cache or disk snapshot for instant paint.
  - now always schedules a durable tail refresh after cache restore.
  - starts SSE and visible polling.
- `refreshTail(...)`
  - calls `sessionMobileTail`, updates detail/events/items, saves cache.
- `lastPubsubSeq`
  - persisted with the cache and passed into the next stream actor.
  - useful for replay when the server process still has the ring buffer.
  - not durable and not sufficient for correctness.

### Web

- `useSessionWorkspace(...)`
  - fetches `/workspace` through React Query.
  - connects to `/workspace/stream?skip_initial=true`.
  - invalidates workspace/projection/session queries on stream change.
  - polls when the stream is disconnected or running tool status needs slow
    reconciliation.

## Design Principles

### Durable Snapshot Owns Correctness

The Runtime Host database/projection is the source of truth for transcript,
pause request, runtime/control state, and projected timeline rows.

Local cache and stream previews may improve perceived speed. They must not
decide that a session is fresh enough to avoid a durable refresh on open.

### Realtime Is Invalidation, Not Transcript Truth

The SSE stream should stay thin. It should wake clients, provide useful
telemetry, and optionally carry a provisional transcript preview. It should not
become a second transcript delivery system.

### Pubsub Sequence Is Not a Durable Cursor

The current pubsub sequence is process-local and ring-buffered. It can help a
foreground client replay recent wakes across reconnects, but it cannot prove
that a cached transcript covers all durable rows.

Do not design correctness around "REST returns latest pubsub seq" unless that
sequence is captured in the same ordering domain as the database snapshot. The
naive version has a race:

1. REST reads the DB snapshot.
2. A new event commits and publishes.
3. REST returns the latest pubsub seq after the publish.
4. Client subscribes from that seq and skips the event that was not in the
   snapshot.

The safe design needs either a durable workspace revision or a stream-side
comparison against the snapshot revision the client actually rendered.

`replay_gap` detection is mandatory, not optional. Pubsub sequences are
process-local and reset after Runtime Host restart, so a persisted client
sequence can be higher than the new process's current sequence or collide with
a newer-but-lower sequence. The gap/ahead signal is what makes persisted replay
cursors safe to keep as an optimization.

### One Viewport Contract

A session detail screen wants one logical payload:

- focused session metadata
- active pause request / question state
- runtime/control state
- projected tail items
- tail pagination/drift anchors
- workspace revision
- optional stream replay cursor

The implementation may keep `/workspace` and `/mobile-tail` during migration,
but they should share a common service-level contract and not encode competing
freshness semantics.

## Target Contract

Add an explicit workspace revision to session viewport responses.

```json
{
  "session": {},
  "projection": {},
  "snapshot_event_id": 12345,
  "workspace_revision": {
    "latest_event_id": 12345,
    "latest_session_updated_at": "2026-06-08T12:00:00Z",
    "latest_runtime_signal_at": "2026-06-08T12:00:01Z",
    "runtime_version_sum": 42,
    "pause_request_version": 7,
    "managed_control_version": 3,
    "live_preview_updated_at": "2026-06-08T12:00:02Z",
    "thread_session_count": 1,
    "fingerprint": "sha256:..."
  },
  "stream_replay": {
    "kind": "process_pubsub_seq",
    "latest_seq": 987,
    "durable": false
  }
}
```

Notes:

- `workspace_revision` is correctness-bearing.
- `stream_replay.latest_seq` is optional and explicitly non-durable.
- REST does not currently return a pubsub sequence. `stream_replay` is new
  surface area and should be deferred unless an implementation proves it adds
  enough latency value; correctness must rest entirely on `workspace_revision`.
- The fingerprint should be derived from the normalized workspace signature,
  not from JSON serialization of the whole response.
- `snapshot_event_id` remains the older-page drift anchor for projection
  pagination.

## Target Stream Handshake

Extend the workspace stream to accept the revision the client believes it has.
The exact wire shape can be query params or headers; the behavior matters more
than the spelling.

Example:

```text
GET /api/timeline/sessions/{id}/workspace/stream
  ?known_workspace_fingerprint=sha256:...
  &skip_initial=true
Last-Event-ID: 987
```

Server behavior:

1. Emit `connected`.
2. Load the current workspace signature unconditionally at connect, before any
   `skip_initial` wait.
3. If the current fingerprint differs, immediately emit `workspace_changed`
   even when `skip_initial=true`.
4. If the fingerprint matches, wait for the next session publish.
5. If pubsub replay is impossible, emit `replay_gap`; client refreshes durable
   tail and continues.

If a client asks for `skip_initial=true` without a known revision, the server
should either ignore `skip_initial` or reject the combination after web/iOS are
migrated. Skipping the initial check without a snapshot revision is the footgun
this epic removes.

This connect-time signature read is the actual snapshot-to-subscribe race fix.
The current stream reads its signature inside the main loop after subscribing;
with `skip_initial=true`, it can skip that first read and wait for a later
publish. Phase 3 must move the comparison before the skip wait.

## Alternatives Considered

### Keep Only Stale-While-Revalidate

Always refresh after cache restore and otherwise leave the stream alone.

This is the current immediate fix. It is low risk and should stay, but it does
not remove the architectural ambiguity. It relies on every future cache path
remembering to refresh.

### SSE Sends the Initial Snapshot

The stream could send the full initial workspace/tail payload, eliminating a
REST call.

Rejected for now. It makes mobile reconnects heavier, duplicates REST response
building inside an SSE lifecycle, and encourages the stream to become a second
transcript transport.

### Remove `skip_initial` Entirely

The stream could always emit an initial `workspace_changed` frame, and clients
could dedupe it against their rendered revision.

This is the simplest correctness-preserving alternative. It removes the
`skip_initial` footgun at the cost of one extra thin stream frame per connect.
Phase 3 must evaluate this as the default before preserving and hardening
`skip_initial`.

### True Transcript Delta Stream

The stream could carry appended projection rows and the client could apply
deltas locally.

Deferred. This is attractive later for speed, but it requires strict ordering,
idempotency, branch/projection semantics, gap recovery, and local mutation logic
on every client. It is unnecessary for the launch reliability bug.

### Poll-Only Session Detail

Remove SSE and poll every few seconds.

Rejected as the primary model. It is simple and robust but worse for latency,
battery, and hosted load. Polling should remain a fallback.

## Implementation Phases

### Phase 0: Guardrail Patch

Status: In local working tree.

Keep the current iOS fix: cached transcript state paints immediately, but
session open always schedules a durable tail refresh.

Acceptance criteria:

- Memory cache restore schedules a background `/mobile-tail` request.
- Disk snapshot restore schedules a background `/mobile-tail` request.
- Refresh failure leaves cached content visible and surfaces a non-blocking
  banner.
- Xcode iOS tests for `SessionViewModelTests`,
  `SessionResumeHydrationTests`, and `SessionStreamResumeTests` pass.
- SwiftUI previews render successfully.

### Phase 1: Name and Test the Invariant

Make the stale-while-revalidate rule explicit in iOS code and tests so future
pause-request/question features cannot accidentally reintroduce cache-as-truth.

Work:

- Rename test cases and helper comments around "cache is instant paint, not
  source of truth."
- Add a notification/deep-link flavored unit test if there is a clean seam for
  it; otherwise add a view-model test that simulates recent cached content plus
  newer network tail.
- Add a small server/client contract note in this spec's implementation notes
  if the actual code uses a different naming convention.

Acceptance criteria:

- A reviewer can find the invariant by searching for
  `stale-while-revalidate` or `source of truth`.
- The iOS tests fail if a cache hit no longer causes a durable refresh.
- No user-visible UI copy is added just to explain the mechanism.

### Phase 2: Add Workspace Revision to Durable Responses

Add a computed `workspace_revision` object to the session viewport payloads.
Start with the payloads that are already used by session detail:

- `/api/timeline/sessions/{id}/mobile-tail`
- `/api/timeline/sessions/{id}/workspace`

Prefer a shared service helper so browser and mobile cannot diverge.

Work:

- Normalize `_load_workspace_signature(...)` into a serializable revision
  object.
- Fix the confirmed pause-request blind spot before relying on the revision.
  Preferred implementation: make pause-request writes bump
  `AgentSession.updated_at` or a dedicated pause/version field in the same
  transaction. Acceptable alternative: add a `SessionPauseRequest` aggregate to
  the signature. Do not proceed with a fingerprint that is blind to questions.
- Verify managed-control state coverage. If managed-control state is visible in
  the viewport and can change independently of runtime state, include it in the
  revision or explicitly document why it is out of scope.
- Add `workspace_revision` to `SessionMobileTailResponse`.
- Add `workspace_revision` to `SessionWorkspaceResponse`.
- Add typed models in Swift and TypeScript generated/OpenAPI surfaces.
- Preserve backwards compatibility for existing clients.

Acceptance criteria:

- The revision changes when durable transcript rows change.
- The revision changes when active pause-request/question state appears,
  changes, or resolves.
- The revision changes when runtime state visible in the viewport changes.
- The revision changes when managed-control state visible in the viewport
  changes, or the spec explicitly records why managed-control state cannot
  change independently of covered runtime/session fields.
- The revision changes when live preview state visible in the viewport changes.
- The revision is stable for two identical viewport reads.
- Backend tests assert the fingerprint differs before vs. after a pause request
  is created and after it is resolved.
- Backend tests cover every field class intended to affect the revision:
  durable events, pause requests, runtime, managed control, and live preview.

Confirmed defect:

- `_load_workspace_signature(...)` does not query pause-request rows.
  Pause-request writes do not currently create an `AgentEvent`, and they should
  not be assumed to bump `SessionRuntimeState` or `AgentSession.updated_at`.
  That means the current signature can miss the exact question UI state from
  the motivating bug. Fixing this is blocking for Phase 3.

### Phase 3: Make `skip_initial` Safe

Extend `/workspace/stream` so skipping the initial workspace event is safe only
when the client supplies a known workspace revision.

Gate: do not start Phase 3 until Phase 2 proves the revision changes for every
viewport-visible state class: durable events, pause requests, runtime, managed
control, and live preview.

Work:

- First evaluate deleting `skip_initial` entirely. If always emitting the first
  thin `workspace_changed` frame is acceptable, prefer that over preserving a
  sharp option.
- Add an optional known workspace fingerprint to the stream request.
- On connect, read the current server fingerprint before any skip wait and
  compare it to the known fingerprint.
- If mismatched, immediately emit `workspace_changed`.
- Keep `Last-Event-ID` pubsub replay as an optimization.
- Keep `replay_gap` behavior; a gap still means "refresh durable tail."
- Decide the compatibility policy:
  - interim: `skip_initial=true` without a known revision behaves as
    `skip_initial=false`;
  - final: reject or remove unsafe `skip_initial=true` calls.

Acceptance criteria:

- A client with a stale revision receives an immediate change event even with
  `skip_initial=true`.
- A client with a current revision does not receive a duplicate initial change.
- A write that lands between REST snapshot and stream connect is detected by
  the revision comparison.
- A server test simulates a snapshot read, commits a viewport-visible write
  before stream connect, and asserts the immediate mismatch event fires.
- Pubsub replay gaps still force durable refresh.
- Existing web/iOS clients continue to work during rollout.

### Phase 4: Adopt the Handshake on iOS

Teach iOS to persist and send the workspace revision in addition to the
non-durable pubsub sequence.

Work:

- Decode `workspace_revision` from `sessionMobileTail`.
- Store it in memory cache and disk snapshot.
- On resume or notification open with a cached revision, issue the durable tail
  refresh and attach the stream with the cached fingerprint in parallel. Either
  a newer REST tail or a stream mismatch can win; do not gate one on the other.
- Start the stream with both:
  - known workspace fingerprint for correctness comparison;
  - last pubsub sequence for opportunistic replay.
- On stream mismatch or replay gap, refresh durable tail.
- Keep the unconditional refresh-after-cache guardrail. The stream handshake is
  additional protection, not permission to trust cache. It is the backstop for
  the high-latency pre-stream-connect window on background resume.

Acceptance criteria:

- Cached open, stream attach, and notification open cannot miss rows that are
  already durable on the server.
- iOS tests cover:
  - cache restore followed by durable refresh;
  - stream mismatch causing refresh;
  - replay gap causing refresh;
  - resume after simulated server pubsub sequence reset;
  - persisted revision surviving view-model recreation;
  - older-page drift behavior still works.
- No regression to lock/unlock behavior or non-blocking refresh failures.

### Phase 5: Adopt the Handshake on Web

Bring the browser session detail hook onto the same freshness semantics.

Work:

- Read `workspace_revision` from the workspace query response.
- Pass the current known fingerprint into `connectSessionWorkspaceStream`.
- Treat stream mismatch as a query invalidation, same as today's
  `workspace_changed`.
- Revisit React Query cache behavior so a cached workspace response cannot be
  silently considered fresh when opening from a notification/highlight link.

Acceptance criteria:

- Web keeps current fast first paint.
- Web does not miss a write that lands between cached query data and stream
  attach.
- Existing render beacon telemetry still records the event that caused the
  refresh.
- Tests in `useSessionWorkspace` and API stream helpers cover the new request
  shape.

### Phase 6: Collapse the Contract Surface

Status: Post-launch hygiene. Not required to prove the freshness invariant.

After both clients use explicit revisions, reduce duplicate semantics.

Work:

- Decide whether to keep both route names or introduce a single
  `/viewport` route.
- If a new route is introduced, make old `/workspace` and `/mobile-tail`
  wrappers over the shared service contract during migration.
- Remove unused iOS `sessionWorkspace` dependencies if the native app only
  needs the compact viewport.
- Keep `/api/agents/*` parity in mind: if the viewport contract becomes
  important to agent clients, add or align the agents route rather than making
  browser-only semantics canonical.

Acceptance criteria:

- There is one service-level builder for the session viewport semantics.
- Browser/mobile route differences are payload-size or auth differences, not
  freshness differences.
- Generated TypeScript and Swift models have matching revision concepts.
- No duplicated pause-request or runtime-state interpretation is added to
  frontend clients.

### Phase 7: Live QA and Regression Harness

Build a small repeatable proof around the original failure mode.

Work:

- Add a fixture or scripted test path that:
  - renders a cached session tail;
  - inserts or serves a newer assistant message plus pause request;
  - opens/resumes the session;
  - verifies the newer rows appear after reconciliation.
- Run the core cached-tail plus newer-message plus pause-request fixture as
  soon as Phase 2 changes the revision primitive. Phase 7 hardens the harness
  and telemetry; it should not be the first proof that the fingerprint can see
  pause requests.
- Add hosted dogfood notes for david010 manual verification if an automated
  fixture is too expensive.
- Record telemetry around cache-hit-to-refresh latency and stream mismatch
  refreshes.

Acceptance criteria:

- The original "notification opens stale cached session and misses question"
  class of bug has an automated or documented regression check.
- The check identifies the exact durable event id and pause-request id that
  must render.
- iOS and web both have at least one test that would fail if `skip_initial`
  could suppress the first durable reconciliation.

## Non-Goals

- Do not replace session archive/projection storage.
- Do not move iOS to machine-token auth.
- Do not make SSE carry full transcript pages.
- Do not add a second transcript delta system for this epic.
- Do not remove polling fallback.
- Do not introduce a hidden "fresh enough" age threshold for cache.

## Decision Log

### Decision: Keep the immediate stale-while-revalidate patch

Context: The reported iOS bug is a real correctness failure in the current
client open path.

Choice: Keep the local patch that always refreshes durable tail after cache or
disk snapshot restore.

Rationale: This closes the bug now without waiting for server contract changes.
It also matches the final invariant: cache is paint, not truth.

Revisit if: A later phase provides a strictly stronger server-driven snapshot
handshake and tests prove cache opens still cannot miss durable rows.

### Decision: Use durable workspace revision for correctness

Context: Pubsub sequence replay is useful but process-local and non-durable.

Choice: Add a revision/fingerprint derived from durable workspace-visible
state, and use it for stream initial-skip safety.

Rationale: It detects the important race: durable data changed after the
client's rendered snapshot, even if pubsub replay cannot prove the exact event
sequence.

Revisit if: Runtime Host later gets a durable monotonic workspace log shared by
REST and SSE.

### Decision: Keep SSE thin

Context: The easiest conceptual fix is to send full workspace data over SSE.

Choice: SSE remains an invalidation/preview channel; REST remains the durable
snapshot channel.

Rationale: Mobile reconnects, backgrounding, auth refresh, and projection
pagination are easier to reason about when only REST owns complete durable
payloads.

Revisit if: A future launch requirement needs sub-100ms full transcript deltas
and the projection model has a durable ordered delta log.

## Review Questions for Hatch Opus

1. Is `workspace_revision` the right correctness primitive, or should the epic
   instead introduce a durable monotonic workspace cursor?
   - Current answer: `workspace_revision` is the pragmatic correctness
     primitive for this epic. A durable monotonic workspace cursor would be
     cleaner but is a larger runtime contract.
2. Does the proposed stream handshake actually close the snapshot-to-subscribe
   race under the current DB + process-local pubsub architecture?
   - Current answer: yes, if and only if the server reads and compares the
     durable fingerprint at connect before any skip wait.
3. Which visible viewport fields are missing from the current
   `_load_workspace_signature(...)` and therefore must be added before relying
   on a fingerprint?
   - Current answer: pause-request state is definitely missing; managed-control
     state must be verified and either included or explicitly scoped out.
4. Should iOS start the stream before or after the first durable refresh when
   it has only a cached revision?
   - Current answer: attach stream and issue durable refresh in parallel on
     resume/notification open.
5. Is Phase 6 worth doing before launch, or should contract collapse wait until
   after the freshness invariant is proven?
   - Current answer: wait. Phase 6 is post-launch hygiene.
6. Are there simpler designs that preserve correctness with less surface area?
   - Current answer: yes. Removing `skip_initial` entirely may be simpler than
     preserving and hardening it; Phase 3 must evaluate that first.

## Suggested Test Commands

Backend:

```bash
make test
```

Frontend:

```bash
make test-frontend
```

iOS:

```bash
xcodebuild test \
  -project ios/XcodeHarness/LonghouseIOS.xcodeproj \
  -scheme Longhouse \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -only-testing:LonghouseIOSTests/SessionViewModelTests \
  -only-testing:LonghouseIOSTests/SessionResumeHydrationTests \
  -only-testing:LonghouseIOSTests/SessionStreamResumeTests
```

SwiftUI previews:

```bash
ios/scripts/render-previews.sh
```
