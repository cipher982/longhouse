# iOS Tiered Launch and Session Loading

## Problem

The iOS app's primary launch job is not "initialize everything." It is:

1. Show the timeline fast enough that the user can immediately scroll.
2. Open a session near the newest transcript content fast enough that the user can read.
3. Fill in lower-priority state after those two surfaces are usable.

The current timeline path is already mostly card-shaped, but the app still waits on a network fetch before showing timeline cards. Session open is heavier: iOS requests a workspace bootstrap with the newest 200 projected items plus thread/runtime/control overlay metadata, decodes all returned event content, builds the render model on the main actor, then sends a large WebKit payload. Tool output is truncated only after the phone downloads and decodes it.

## User Contract

### App Launch

Opening the app should paint the last known timeline immediately if authenticated state is known locally. The live network refresh should replace it in place. Push registration, widget refresh, notification cleanup, settings fetches, and action/composer readiness must not block the first scrollable timeline.

### Timeline Scroll

Timeline cards are summary cards. They should include only:

- title / summary / first-user fallback
- provider, project, branch, origin, runtime badges
- counts, recency, capabilities
- enough stable IDs to open the session

Timeline cards must not include transcript pages.

### Session Open

Tapping a session should load a recent tail page first, not a broad workspace page. The first readable page should be biased toward the newest messages because the transcript snaps to the bottom.

Default mobile tail size: 50 projected items.

The app should then:

- render tail page
- preserve bottom stickiness
- start SSE for future updates
- prefetch older pages when idle or when the user scrolls near the top
- load controls/actions after the transcript is readable

### Older Transcript

Older transcript content is paged backward from the tail. When the user scrolls near the top, the app should fetch the previous page and prepend it without moving the user's viewport.

### Tool Detail

Tool output should be summarized/truncated server-side for mobile initial pages. Full tool input/output can load on explicit expansion or follow-up detail fetch. Mobile should not pay the full payload cost for collapsed rows.

## Desired Loading Tiers

### Tier 0: Cached Timeline

Read cached timeline cards from local storage before the first network refresh.

Storage:

- app-local cache, not the widget's active-session-only snapshot
- keyed by server URL and authenticated user/session identity when available
- bounded to the newest 40 cards
- ignored if older than 24 hours or from a different server
- versioned payload with bump-on-reject migration behavior
- cleared on sign-out and server URL change

Behavior:

- if cache exists, show cached cards immediately with stale/fresh connection state
- still run `restoreSession()` when required
- network refresh updates the cache and UI in place
- do not reuse `WidgetSessionSnapshotStore`; it intentionally filters to active sessions for widget display

### Tier 1: Fresh Timeline

Fetch `/api/timeline/sessions?days_back=14&limit=40`.

Rules:

- one in-flight first-load refresh per timeline view model
- immediate refreshes use a `refreshTask`/in-flight guard; pull-to-refresh can force a new request
- do not duplicate refresh from `.task`, `.onAppear`, and `scenePhase == active`
- write successful responses to `TimelineCacheStore`
- widget reload is snapshot-driven and throttled

### Tier 2: Session Tail

Use `/api/timeline/sessions/{session_id}/mobile-tail?anchor=tail&limit=50&branch_mode=head&payload=mobile` for initial transcript rows, or equivalent projection-backed response. The response must include:

- focused session detail/capabilities needed for title, composer placeholder, and runtime dock placeholders
- projection items for the recent tail
- paging metadata
- mobile-trimmed event payloads

Rules:

- render projection items directly into the transcript model
- initial render should not require full thread metadata
- hydrate nonessential controls/thread detail after the transcript is readable
- optimistic/provisional submitted input reconciliation still runs against the loaded tail; unmatched optimistic rows remain visible until a later refresh includes their durable event

### Tier 3: Session Prefetch

After timeline first paint and a short idle window:

- prefetch tail pages for the top 2-3 visible/recent cards
- max 2 concurrent prefetches
- cancel prefetch on `scenePhase != active`
- cancel outstanding prefetches when the user taps a session
- use a short request timeout so prefetch never starves the foreground open
- never block scroll

### Tier 4: Older Page Fetch

When the WebKit transcript reports near-top scroll:

- fetch `projection?anchor=tail&limit=50&offset=<loaded_tail_count>`
- prepend returned items
- preserve scroll position
- continue until `loaded_count >= total`
- detect drift when total/page boundaries change during live writes; refetch the current tail and reset older-page state if the server reports an incompatible snapshot

### Tier 5: Deferred Controls

Composer/action readiness can arrive after readable transcript:

- session capabilities
- loop mode
- live activity state
- detailed thread continuation metadata

The UI can show a disabled composer or lightweight placeholder while transcript text is already readable.

## Server Contract Changes

Use existing projection internals before adding a separate paging stack, but do not assume the current HTTP contract is enough. Today `anchor=tail` ignores user-supplied offset because `load_from_end=True` rewrites the offset to `total - limit`; older-page tail pagination is a real server change.

Needed additions:

1. Tail pagination semantics.
   - `anchor=tail&offset=0` means newest page
   - `anchor=tail&offset=50` means the 50 items immediately older than the newest 50
   - implementation should calculate `effective_offset = max(0, total - limit - user_offset)`
   - response remains chronological within the page
   - `page_offset` identifies absolute projection offset for debugging

2. Snapshot drift handling.
   - include a stable snapshot marker such as `as_of_event_id` or equivalent projection signature
   - older-page requests include the marker
   - if the marker no longer matches, server returns a typed drift response or client treats changed marker as a reset signal

3. A mobile payload mode for projection/event responses.
   - omit or server-truncate `tool_output_text`
   - default mobile tool output budget: 2,000 characters per event
   - threshold-strip large `tool_input_json`; keep small JSON needed for useful one-line summaries
   - include truncation metadata when output/input was reduced
   - preserve enough fields for pairing and display

4. A lightweight mobile tail envelope.
   - session detail/capability fields required by iOS session chrome
   - projected transcript page
   - no full thread/session list unless explicitly requested
   - no heavy overlay fields unless needed for the first readable screen

5. Full tool detail on demand.
   - add a concrete route or query mode for fetching one full event/tool payload later
   - initial mobile transcript must not require this route

## iOS Implementation Slices

### Slice A: Timeline Cache

Files:

- `ios/Sources/Shared/TimelineCacheStore.swift`
- `ios/Sources/LonghouseApp/InboxView.swift`
- tests in `ios/Tests/LonghouseIOSTests/`

Changes:

- save successful timeline responses
- load cached cards before network
- prevent duplicate first-load refreshes
- add OSLog markers: `timeline_cache_hit`, `timeline_first_paint`, `timeline_refresh_finished`
- add telemetry beacons alongside existing render diagnostics where practical

### Slice B: Session Tail API

Files:

- `ios/Sources/Shared/LonghouseAPI.swift`
- `ios/Sources/Shared/SessionAPIAdapters.swift`
- `ios/Sources/LonghouseApp/SessionView.swift`
- tests in `SessionViewModelTests`

Changes:

- add `sessionMobileTail(id:anchor:limit:offset:branchMode:snapshot:)`
- add `SessionViewModel` tail state: loaded count, total, page offset, has older page
- initial open loads `limit=50&anchor=tail`
- keep workspace load only for compatibility/debug fallback if needed, not default
- SSE `workspace_changed` refreshes the mobile tail, not the old workspace limit 200 path
- detail/control hydration must be separate from transcript readability

### Slice C: Older Page Prepend

Files:

- `WebTranscriptView.swift`
- `SessionView.swift`

Changes:

- report top-scroll proximity from WebKit to Swift
- trigger older-page fetch before user reaches the top
- prepend items and preserve scroll offset
- add `WKUserContentController` + JS-to-Swift scroll bridge
- add a JS prepend entrypoint rather than replacing `root.innerHTML` for older pages
- guard duplicate payloads and preserve bottom-stickiness for live/tail refreshes

### Slice D: Background Prefetch

Files:

- `InboxView.swift`
- `LonghouseAPI.swift`

Changes:

- after fresh timeline load, prefetch top 2-3 session tails
- cache prefetched projections in memory
- consume cache on session open if fresh
- cancel on background and on foreground session open

### Slice E: Mobile Payload Mode

Files:

- `server/zerg/routers/timeline.py`
- `server/zerg/routers/agents_sessions.py`
- `server/zerg/services/session_views.py`
- tests in `server/tests_lite/`

Changes:

- add query parameter such as `payload=mobile`
- server-truncate tool output for mobile projection responses
- include truncation metadata if the UI should expose "load full output" later
- update projection/tail tests for `anchor=tail&offset=N`
- update SSE-driven iOS refresh path to avoid workspace re-bloat

## QA Harness

### Before/After Simulator Harness

Use existing local database export patterns from `make test-mobile-chat-replay`.

Add or extend a simulator UI test path that:

1. Seeds or exports a large session from local DB.
2. Launches the app with a cached timeline fixture.
3. Measures:
   - launch task duration
   - timeline cache paint duration
   - fresh timeline refresh duration
   - session tail first render duration
   - WebKit payload bytes / row count / render duration
4. Opens the heavy session and scrolls up enough to trigger older-page fetch.

Acceptance:

- cached timeline appears before network refresh
- initial session open requests 50 projected items, not workspace 200
- initial WebKit payload is under 200 KB on the standard heavy fixture unless the fixture intentionally exceeds the budget
- session tail first render is under 600 ms on the simulator fixture after the network response arrives
- older page loads without visible scroll jump
- SSE update does not issue `workspace?limit=200`

### Device Pass on `olive`

After simulator tests pass, install from Xcode and inspect logs:

- `Startup`
- `Timeline`
- `WebTranscript`
- `WidgetAuth`

Device-only checks:

- APNS registration is trailing work
- widget process does not cause launch hitch
- scroll feels stable with debug build overhead
- cold launch
- warm launch from background after more than 10 minutes
- session open from timeline card
- session open from notification tap
- session open from widget tap if widget deep-linking is enabled

## Non-Goals

- Redesign the session UI.
- Change timeline ranking.
- Make every session action instant.
- Load full tool output by default on mobile.
- Build a new backend pagination stack when projection internals already provide the base ordering/windowing model.

## Open Questions

1. Whether the mobile tail envelope is a new route or a projection query mode. Prefer a new route if schema differences are meaningful; prefer query mode if generated clients remain simple.
2. Whether iOS should keep a disk cache of session tail pages. Start with memory-only prefetch; add disk only if device traces show repeated session-open latency.
