# Mobile Chat Hardening Plan

Status: Active plan
Owner: Longhouse iOS / session workspace
Updated: 2026-05-19

## Summary

The iOS session view now uses a single WebKit transcript renderer surrounded by
native SwiftUI chrome, runtime controls, and composer. The next goal is not a
new rewrite; it is to make this chat surface measurable, testable, and boring
enough to dogfood without relying on manual QA.

David is the only developer and primary dogfood user, so every improvement
should either remove duplicate behavior, add automated coverage, or expose
debug evidence for the next issue.

## Operating Principles

- Keep iOS on the WebKit transcript path. Do not reintroduce a parallel native
  transcript renderer.
- Prefer fixture-backed tests over manual "try it on the phone" checks.
- Add observability before speculative tuning when the failure mode is about
  freshness, scroll behavior, or perceived smoothness.
- Keep native controls native: title, runtime dock, loop mode, composer,
  keyboard behavior, live activity controls.
- Commit and push small slices on `main`; no feature branches or worktrees for
  this hardening track unless the working tree becomes unsafe.

## Current Baseline

Already in place:

- Web and iOS emit client render beacons to `/api/telemetry/client-render`.
- The server persists client render observations and exposes
  `/api/telemetry/client-render/recent` for admin forensic debugging.
- The session detail page has a render telemetry panel for recent web/iOS
  render beacons on the current session.
- The hot-plane e2e path asserts persisted render telemetry.
- iOS fixture UI tests cover initial bottom pinning, live updates, keyboard
  updates, streaming updates, optimistic send, origin marker display, and large
  transcript scrolling.
- Web and iOS both reconcile Longhouse-authored input by durable identity
  instead of raw text matching.

Known gaps:

- iOS render beacons currently use event timestamps but do not expose WebKit
  payload/render diagnostics such as payload size, row count, JS failures, or
  scroll-stick decisions.
- The high-value mobile chat validation subset is still spread across ad hoc
  commands instead of one focused target.
- The WebKit transcript UI is functional but still needs product polish for
  dense tool rows, long-message expansion, copy/text selection, and active tool
  state.
- The composer still needs more automated coverage around queue vs steer,
  failed-send retry, app backgrounding, and reconnect timing.

## Order Of Operations

### 1. Make render telemetry usable

Build a narrow debug surface over the telemetry that already exists.

Status: Done.

Deliverables:

- Add a small admin/debug view or session-local panel that shows recent render
  beacons for the current session.
- Show latest rendered event id, surface, managed flag, latency, observed time,
  and clock skew.
- Add backend/web tests for the view or API adapter.

Done when:

- From a session detail page, a developer can answer "did this event render on
  web or iOS, and how stale was it?" without querying the DB manually.

### 2. Add WebKit transcript diagnostics

Extend the iOS WebKit renderer and beacon payload path with lightweight
debug-only diagnostics.

Deliverables:

- Track transcript payload byte size, row count, latest item id, render
  sequence, and JavaScript evaluation failure count inside `WebTranscriptView`.
- Surface those diagnostics in DEBUG builds or a hidden diagnostics affordance.
- Add iOS unit coverage for payload construction where possible and fixture UI
  coverage for visible diagnostic state if exposed.

Done when:

- A dogfood screenshot or debug readout can distinguish "server did not send
  it", "Swift did not pass it to WebKit", and "WebKit failed to render it".

### 3. Strengthen mobile chat smoke coverage

Add deterministic fixtures that represent the actual dogfood shape: user
input, Longhouse origin, active tool calls, completed tool calls, assistant
streaming, queue state, and ended-session state.

Deliverables:

- One compact "real mobile chat" fixture used by iOS UI tests and preview
  rendering.
- UI assertions for Longhouse-origin user rows, active tool rows, dropped tool
  rows, submitted optimistic rows, and keyboard-open layout.
- Preview images for dark and light mode with the same fixture.

Done when:

- A renderer or composer regression fails locally before David finds it on his
  phone.

### 4. Polish the WebKit transcript

Use the telemetry and fixtures to make the transcript feel intentional rather
than merely functional.

Deliverables:

- Tighten tool-row density and metadata truncation on iPhone widths.
- Improve running/dropped/completed tool states.
- Improve long-message expansion and preserve readable copy/select behavior.
- Keep the Longhouse origin marker subtle, semantic, and non-XML.
- Verify with iOS UI tests and rendered previews.

Done when:

- The iOS transcript looks and behaves like a first-class mobile chat surface,
  not an embedded desktop transcript.

### 5. Harden composer and lifecycle edges

Target the cases most likely to hurt mobile dogfooding.

Deliverables:

- Tests for send while streaming, queue while running, failed send then retry,
  app background/reopen, SSE disconnect fallback, and ended-turn decision flow.
- Any missing user-visible states in the composer or runtime dock.
- No text-matching reconciliation fallbacks.

Done when:

- The composer behavior remains deterministic through common mobile lifecycle
  interruptions.

### 6. Promote the checks into routine ship safety

Make the high-value mobile chat checks easy to run and hard to forget.

Deliverables:

- A focused `make` target or documented command group for mobile chat hardening.
- CI coverage for the fastest stable subset.
- Keep heavier simulator/UI checks available locally and in GitHub Actions.

Done when:

- A normal chat-interface change has an obvious, repeatable validation path.

## First Slice

Start with the focused validation target. The render telemetry surface is now
in place, so the next smallest infrastructure step is `make test-mobile-chat`:
web render telemetry tests plus the iOS Longhouse unit-test scheme, without the
heavier UI smoke path. This gives the diagnostics, fixture, and polish work an
obvious local validation loop.
