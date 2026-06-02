# Timeline Connectivity Honesty

Status: Draft, initial Opus review incorporated
Last updated: 2026-06-02
Owner: maintainer

## One-sentence summary

The iOS timeline can show `Reconnecting` or `Offline` when only the realtime
SSE subscription churned, even while normal timeline REST refreshes are
succeeding. That is a false product-health claim.

## Problem

The timeline has two lanes:

- **Snapshot lane**: `GET /api/timeline/sessions` returns the durable current
  list. This is the correctness lane.
- **Realtime lane**: `/api/timeline/sessions/stream` pushes changes and
  heartbeats. This is an optimization lane.

The current iOS timeline collapses both lanes into one counter:

- REST refresh failures increment `consecutiveRefreshFailures`.
- SSE `.disconnected(error)` also increments `consecutiveRefreshFailures`.
- `ConnectionState.derive(failures:lastUpdatedAt:)` turns one warm failure into
  `reconnecting` and two into `offline`.
- `ConnectionStatusStrip` renders those as yellow/red global warnings.

The most important failing path is active stream churn: the SSE response ends or
is recycled while the view is still active, `TimelineSessionsStream` emits
`.disconnected(nil)` or a transport error, and the view model briefly promotes
that into a user-visible warning before reconnect/bootstrap evidence arrives.
Two such events before a reset can become `offline`.

Intentional view/app lifecycle stops are narrower: `stopStream()` bumps
`streamGeneration` before cancelling, and `handleStreamEvent` drops stale
generation events. That guard already neutralizes many client-stop cancellation
events. The implementation still needs tests around that boundary, because
`URLSession.AsyncBytes` can surface cancellation as either Swift cancellation or
`URLError(.cancelled)`.

A user-visible `Offline` banner should mean the timeline cannot keep itself
fresh, not that one long-lived transport was recreated.

## Research baseline

Primary references:

- WHATWG HTML, Server-sent events:
  `https://html.spec.whatwg.org/multipage/server-sent-events.html`
- MDN, Using server-sent events:
  `https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events`
- Apple, `URLSessionTask.cancel()`:
  `https://developer.apple.com/documentation/foundation/urlsessiontask/cancel()`
- Apple, `URLSessionConfiguration.waitsForConnectivity`:
  `https://developer.apple.com/documentation/foundation/urlsessionconfiguration/waitsforconnectivity`

Findings:

- SSE reconnect is normal. The EventSource processing model explicitly has a
  `CONNECTING` state for a connection that was closed and is reconnecting.
- Browser EventSource only stops reconnecting for terminal cases such as
  explicit close/fatal failure; HTTP 204 is the server-side "stop" signal.
- SSE supports `Last-Event-ID`, but a timeline projection can also be robust by
  treating the stream as an invalidation lane and resyncing via the snapshot
  lane after reconnect.
- Periodic comments or heartbeat events are standard keepalive mechanisms for
  idle event streams.
- On Apple platforms, cancelling a URLSession task reports
  `NSURLErrorDomain` / `NSURLErrorCancelled`. That error is expected for
  intentional client lifecycle cancellation and is not itself evidence that the
  network is unavailable.
- `waitsForConnectivity` means a URLSession task can wait for suitable
  connectivity rather than failing immediately; waiting is a distinct state
  from product-level offline.

## Current code map

Server:

- `server/zerg/services/timeline_session_stream.py`
  - emits `connected`
  - emits `session_upsert` / `session_remove`
  - emits `heartbeat` roughly every 30s
  - intentionally does not provide timeline stream replay; callers resync by
    snapshot

Web:

- `web/src/hooks/useTimelineSessionStream.ts`
  - applies stream upserts/removes into the React Query cache
  - has slow polling reconciliation in `SessionsPage`
  - does not display a global offline banner from timeline stream errors

iOS:

- `ios/Sources/Shared/TimelineSessionsStream.swift`
  - manually parses SSE via `URLSession.AsyncBytes`
  - runs its own reconnect loop with backoff
  - has a 45s stale watchdog, because server heartbeats every 30s
  - emits `.disconnected(error)` for active non-terminal stream endings
- `ios/Sources/LonghouseApp/InboxView.swift`
  - treats every `.disconnected` as a refresh failure
  - uses the same failure count for REST and stream failures
  - renders the global strip from that derived state

## Design principles

1. **Transport state is not product health.** A stream can reconnect without the
   timeline being degraded.
2. **Snapshot success is stronger reachability evidence than stream failure.**
   If REST refreshes are succeeding, the app is not offline.
3. **Freshness is the user-facing concern.** Users care whether the list is
   current enough to trust, not whether one transport is currently open.
4. **Offline is a high-severity claim.** Render it only after sustained evidence
   that the app cannot reach the server or cannot refresh data.
5. **Auth is terminal and separate.** `401` / expired session means sign in
   again, never `Offline`.
6. **Lifecycle cancellation is expected.** View disappearance, scene
   backgrounding, and intentional task cancellation must not increment outage
   counters.
7. **Recovered failures should clear quickly.** A successful snapshot or stream
   event should clear degraded/offline state without waiting for a timer.
8. **The state machine must be testable without network.** UI severity should be
   a pure derivation from observed facts.

## Target model

Use the smallest state model that preserves the product truth:

1. Store **snapshot reachability** as the product-health input.
2. Derive **freshness** from `lastUpdatedAt` and an injected clock.
3. Derive the visible banner from those facts.
4. Treat stream transport as diagnostics only.

### `SnapshotReachability`

Stored in the iOS timeline view model:

- `unknown` — no snapshot attempt yet
- `reachable` — latest snapshot succeeded
- `degraded` — latest snapshot failed, but recent successful data exists
- `offline` — repeated snapshot failures and no fresh data, or OS path says no
  network and no usable fresh data
- `authRequired` — snapshot or stream got auth failure

Only snapshot results, auth results, and explicit OS no-connectivity evidence may
write this state. Stream disconnects do not.

### Freshness

Pure derivation:

```swift
freshness = f(lastUpdatedAt, now, hasLoadedData)
```

Suggested thresholds are implementation constants, not product vocabulary:

- `fresh`: recently updated enough to trust silently
- `aging`: old enough to show low-severity `Updating timeline...` if recovery is
  active
- `stale`: too old to present as current while snapshots are failing
- `unknown`: no successful snapshot/cache/stream event yet

The reducer must take a clock so tests can prove boundary behavior.

### `TimelineConnectivityBanner`

Derived output:

- `none` — normal state; includes hidden stream reconnects while data is fresh
- `updating` — low-severity stale/aging state while recovery is active
- `degraded` — timeline is stale and snapshots are failing, but cached data is
  still usable
- `offline` — sustained snapshot failure or OS no-connectivity evidence with no
  trustworthy fresh data
- `authRequired` — session expired

`Reconnecting` should not be a global banner for the timeline. It can exist in
debug logs and diagnostics. If visible at all, it belongs in a debug surface.

Banner derivation:

| Snapshot reachability | Freshness | Banner |
|---|---|---|
| `authRequired` | any | `authRequired` |
| `unknown` | `unknown` | `none` |
| `unknown` | `fresh` / `aging` | `none` |
| `unknown` | `stale` | `updating` if recovery active, otherwise `none` (recovery now only flips on snapshot failure, which also moves reachability off `unknown`, so this cell is effectively `none` in practice) |
| `reachable` | `fresh` / `aging` / `stale` | `none` |
| `degraded` | `fresh` | `none` |
| `degraded` | `aging` | `updating` |
| `degraded` | `stale` | `degraded` |
| `offline` | `fresh` | `none` |
| `offline` | `aging` | `updating` |
| `offline` | `stale` / `unknown` | `offline` |

`reachable + stale` is intentionally silent: if the snapshot lane just
succeeded, the server was reachable and the snapshot is the current durable
truth, even if the sessions inside it are old. A later product decision can
explain "no recent session activity" elsewhere; it is not a connectivity error.

### `StreamDisconnectReason`

Classified diagnostic input, not product-health state:

- `clientStop`
- `watchdogStop`
- `serverEOF`
- `networkError`
- `cancelled`
- `authFailure`
- `waitingForConnectivity`
- `unknown`

The raw `Error?` is not enough to infer this. `stopStream()` and watchdog code
must pass intent into the classifier.

## Event rules

Rules are ordered by strength of evidence.

1. Any auth failure sets `SnapshotReachability.authRequired`, which derives
   `TimelineConnectivityBanner.authRequired`.
2. Any successful snapshot sets `SnapshotReachability.reachable`, clears
   snapshot failure counters, and refreshes `lastUpdatedAt`.
3. Data-bearing stream events (`session_upsert` / `session_remove`) refresh
   `lastUpdatedAt`; transport-only events (`connected` / `heartbeat`) do not.
   - On reconnect, wait for the snapshot bootstrap or a data-bearing stream
     event before stamping freshness.
4. Intentional lifecycle stops are diagnostics only and do not alter snapshot
   reachability or user banner.
5. Stream disconnects are diagnostics only and do not alter snapshot
   reachability or user banner.
6. A stale-stream watchdog should force a reconnect; it should not render
   `Offline` unless snapshots also fail or data becomes stale.
7. Snapshot failures drive product severity. One failure with fresh data is
   silent. Repeated failures with stale data become `degraded`; sustained
   failure with no usable data becomes `offline`.
8. OS no-connectivity evidence from `NWPathMonitor` may accelerate `offline`,
   but only when no trustworthy fresh data is available.
9. Recovery is edge-triggered: a data-bearing stream event or successful
   snapshot clears visible degraded/offline state as soon as the state is
   trustworthy.

## UX contract

Normal steady state:

- No strip in release builds.
- Timeline cards keep their normal runtime/attention styling.

Cold start:

- The loading/empty/error content owns cold-start presentation.
- Do not show a connectivity strip for `SnapshotReachability.unknown` with no
  loaded data unless auth has already failed.

Hidden reconnect:

- No strip while data is fresh.
- Optional debug log: stream generation, error code, retry delay.

Stale but recovering:

- Low-severity strip copy: `Updating timeline...`
- This is appropriate when data is aging/stale and a retry or snapshot is in
  flight.

Degraded:

- Yellow strip copy: `Timeline may be stale`
- Secondary copy in diagnostics/logs can include last successful refresh age.

Offline:

- Red strip copy: `Offline`
- Only when snapshot reachability is failing and freshness is no longer
  trustworthy.

Auth:

- Explicit sign-in copy. Do not reuse offline styling.

## Instrumentation requirements

Every stream lifecycle log should include:

- stream generation
- scene phase / lifecycle reason when available
- disconnect reason
- error domain/code
- whether the disconnect was client-initiated, watchdog-initiated, server EOF,
  auth, or unknown
- retry delay
- last stream event age
- last snapshot success age
- derived user banner

Every snapshot log should include:

- elapsed time
- HTTP/auth/error classification
- consecutive snapshot failures and reachability state
- last stream event age
- derived user banner

Add a structured debug event for banner transitions so field logs can answer:
"what exact evidence made the app warn the user?"

## Implementation plan

### Phase 1 — Model and tests only

- Extract a pure timeline connectivity reducer in iOS.
- Inputs: snapshot result, `lastUpdatedAt`, loaded-data presence, optional
  OS-path status, classified stream diagnostic event, current time.
- Outputs: `SnapshotReachability` and `TimelineConnectivityBanner`.
- Define `StreamDisconnectReason` as an already-classified diagnostic enum.
  Raw `URLError` mapping happens later.
- Add unit tests for:
  - active EOF/proxy-churn cycle `.disconnected(nil)` -> `.connected` ->
    snapshot success, repeated -> no degraded/offline banner while data is fresh
  - repeated stream cancellations while snapshots succeed -> no banner
  - stream watchdog reconnect while data fresh -> no banner
  - stream errors plus stale data but successful snapshot -> no offline
  - repeated snapshot failure plus stale data -> degraded/offline
  - auth failure -> authRequired
  - background/foreground stop/start -> no failure count
  - generation-guard boundary: stale-generation disconnect cannot mutate product
    health
  - `waitsForConnectivity`/waiting diagnostic does not equal offline
  - freshness thresholds use an injected clock
- Replace the existing `ConnectionStateTests` ladder tests; the old
  `failures:lastUpdatedAt` contract is intentionally retired.

### Phase 2 — Wire iOS timeline to the reducer

- Replace `consecutiveRefreshFailures` as the source for `ConnectionState`.
- Keep the existing visual strip component initially, but feed it from
  `TimelineConnectivityBanner`.
- Classify URLSession/URLError values before they reach policy.
- Add lifecycle reason tracking to `startStream` / `stopStream` / scene phase.
- Track watchdog cancellation intent explicitly; do not infer it from the
  resulting URLSession error type.
- Add `NWPathMonitor` input only as OS no-connectivity evidence, not as a generic
  reachability replacement for snapshot results.
- Add instrumentation from the spec.

### Phase 3 — Tune stream correctness and server contract

- Keep timeline stream as an invalidation lane unless evidence shows snapshot
  bootstraps are too expensive.
- Assert server heartbeat cadence stays below deployed proxy idle timeouts.
- Confirm headers disable buffering for hosted and self-hosted paths.
- Keep snapshot requests on short request timeouts; do not give the correctness
  lane the stream's long wait-for-connectivity/resource timeout.
- Consider stream `id` / `Last-Event-ID` only if it removes measurable
  snapshot load without weakening correctness. The default remains snapshot
  resync on reconnect.

### Phase 4 — Align web diagnostics

- Web does not currently show the false global banner, so no UX change is
  required.
- Add equivalent stream/snapshot debug events if we want parity in field
  diagnosis.
- Keep slow polling reconciliation as the web correctness backstop.

### Phase 5 — Visual QA and dogfood

- Add iOS previews or screenshot fixtures for hidden/aging/degraded/offline/auth
  states.
- Run focused iOS unit tests.
- Run SwiftUI preview rendering for touched views.
- Install on a phone and reproduce:
  - idle foreground on good WiFi
  - background/foreground
  - airplane mode
  - server auth expiry
  - hosted stream restart / proxy recycle if easy to simulate

## Acceptance criteria

- Active stream EOF/proxy recycle, including repeated `.disconnected(nil)`
  events followed by reconnect/bootstrap, does not produce a degraded/offline
  strip while data remains fresh.
- The log pattern `timeline stream disconnected error=cancelled` interleaved
  with successful `timeline refresh finished` does not produce a
  degraded/offline strip.
- The timeline still turns visibly degraded/offline when REST snapshots cannot
  refresh and cached data is stale.
- Auth expiry is rendered as sign-in-required, not offline.
- Backgrounding or navigating away does not increment product-health failure
  counters.
- A data-bearing stream event or REST snapshot clears visible warnings.
- Field logs can explain every visible banner transition from concrete evidence.
- The reducer is clock-injected and covered at freshness thresholds.

## Review findings

Initial Hatch Opus review, 2026-06-02:

- The original draft correctly identified a category error, but over-modeled the
  fix with four stored axes. This revision collapses product policy to
  `SnapshotReachability` plus clock-derived freshness, and demotes stream
  transport to diagnostics.
- The actual active bug path is active stream EOF/proxy churn, not necessarily
  lifecycle cancellation; the existing `streamGeneration` guard already drops
  many stale cancellation events. This revision names both paths and requires
  tests for the guard boundary.
- Raw URLSession errors cannot encode lifecycle intent. This revision adds
  `StreamDisconnectReason` and requires watchdog/client-stop intent to be
  tracked before classification.
- Offline detection must not rely on long-lived SSE timeouts. This revision
  keeps snapshots on their existing short request timeout and allows
  `NWPathMonitor` only as an explicit OS no-connectivity input.

Second Hatch Opus review, 2026-06-02:

- No blocking findings.
- Added the banner derivation table, first-connect freshness note, cold-start
  behavior, and explicit replacement of the old `ConnectionStateTests` ladder.

Final holistic Hatch Opus review, 2026-06-02:

- No release-blocking findings. `Offline`/`degraded`/`Reconnecting` are provably
  unreachable from any stream signal or disconnect; only snapshot failure, auth,
  or OS-path-unsatisfied can reach those reachability states.
- Fixed one residual contract impurity (Rule 5): `.streamDisconnected` used to
  set `recoveryActive = true`, which in the `unknown + stale` cold-start cell
  could surface an `Updating` strip from pure transport churn. Recovery is now
  owned solely by snapshot failures (real product-health evidence) and cleared
  by snapshot success / data-bearing stream events. Added a regression test
  (`streamDisconnectAloneNeverDrivesAVisibleBanner`).
- Deferred (non-blocking, conservative-only): Phase 2's `NWPathMonitor` input
  and explicit watchdog/`waitsForConnectivity` disconnect-reason wiring are
  modeled and unit-tested in the reducer but not yet wired into the live view
  model. The live classifier maps watchdog cancellation to `.cancelled`. These
  paths can only ever *escalate* toward offline, never produce a false offline,
  so shipping without them does not reintroduce the bug. The reducer keeps the
  `.watchdogStop`/`.waitingForConnectivity`/`.networkPathChanged` surface ready
  for that wiring.
- Deferred (non-blocking): full instrumentation from the "Instrumentation
  requirements" section (structured banner-transition debug events, last
  snapshot/stream age on every log line) is lighter than specified. Current
  logging covers refresh/disconnect lifecycle but does not yet fully satisfy
  "field logs can explain every visible banner transition." Worth completing
  before wider rollout beyond dogfood.

## Non-goals

- Replacing SSE with WebSockets.
- Building a generic network reachability framework.
- Making the timeline stream the durable source of truth.
- Showing provider/machine control-path offline state in the global timeline
  connectivity strip; session runtime display already owns that surface.
