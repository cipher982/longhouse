# iOS Session Workspace Contract

Status: Proposed
Owner: iOS companion / session workspace
Updated: 2026-05-02

## Goal

Make iOS session detail consume the same session workspace contract as web:
one focused session, one thread summary, and one projected transcript page.

iOS was added after the web workspace and grew its own session-detail loading
path. That path currently fetches session metadata and raw events separately,
then rebuilds transcript state in the client. The crash fixed in May 2026 was
triggered by that split async load path. The broader launch issue is contract
drift: iOS can disagree with web about what a session workspace is.

## Product Contract

Opening a session on iOS should mean:

- load `/api/timeline/sessions/{session_id}/workspace`
- render `session` as the focused metadata/control state
- render `projection.items` as the transcript source
- use `thread` for continuation/head context when iOS exposes it
- subscribe to `/api/timeline/sessions/{session_id}/workspace/stream`
- on stream change, refetch the workspace payload

Raw `/events` remains useful for backend/API compatibility, but it is not the
session-detail bootstrap contract. The iOS app should not keep split
`sessionDetail`/`sessionEvents` helpers after the migration unless a real,
explicit caller remains.

## Why Now

Pre-launch, a clean contract change is cheaper than supporting two mobile
interpretations later. This also aligns with the product invariant from
`VISION.md`: human clients are views over the same session model, not separate
sources of truth.

## Boundary Rules

- The backend owns workspace assembly.
- Routers own authentication and route shape only.
- Web and iOS both consume the timeline workspace route for user-cookie flows.
- `/api/agents/*` remains the canonical machine surface, but the iOS app is a
  user-auth client, not a machine-token client.
- iOS may keep fallback display logic for old payloads, but new fields should
  be added to backend response models first.
- No hidden client fallback from workspace to split detail/events in normal app
  flow. A workspace failure should surface as a session load failure.
- Stream or polling refresh failures should not clear the last good workspace
  state; initial load failures should surface.

## Target Backend Shape

Move workspace assembly out of `agents_sessions.py` into a service function,
for example:

```python
build_session_workspace(
    db: Session,
    session_id: UUID,
    branch_mode: str = "head",
    limit: int = 100,
) -> SessionWorkspaceResponse
```

Then:

- `/api/agents/sessions/{id}/workspace` verifies machine auth and calls the
  builder.
- `/api/timeline/sessions/{id}/workspace` verifies browser/user auth and calls
  the builder.
- Both routes return the same `SessionWorkspaceResponse` model.

This removes the current router-to-router delegation where the timeline route
calls the agents route implementation with auth dependencies set to `None`.
This extraction is a hygiene and parity step, not a behavior change. It must be
mechanical: preserve response shape, `Cache-Control`, and server timing.

## Target iOS Shape

Add Swift models mirroring the existing response:

- `SessionThreadResponse`
- `SessionProjectionItem`
- `SessionProjectionResponse`
- `SessionWorkspaceResponse`

Add:

```swift
func sessionWorkspace(
    id: String,
    limit: Int = 200,
    branchMode: String = "head"
) async throws -> SessionWorkspaceResponse
```

`SessionViewModel.refreshWorkspace` should call only `sessionWorkspace`.

Initial rendering can map `projection.items.compactMap(\.event)` into the
existing `TimelineBuilder` to keep the UI change small. A follow-up can render
seam rows directly so iOS matches web continuation display.

## Phases

### Phase 1: Backend Workspace Builder

- Extract workspace assembly from `agents_sessions.get_session_workspace`.
- Keep response model and route paths unchanged.
- Add or update backend tests for both agents and timeline routes.
- Add a parity test proving both routes return the same body for the same
  session.

Acceptance:

- Existing web workspace behavior is unchanged.
- Both routes return `session`, `thread`, and `projection`.
- Invalid `branch_mode` and missing session behavior stay unchanged.
- `Cache-Control` and server timing headers are preserved.

### Phase 2: iOS Workspace Decode And API

- Add iOS workspace response models.
- Add `LonghouseAPI.sessionWorkspace`.
- Add decoder tests using a representative workspace JSON payload with at least
  one event item and one seam item.
- Use `limit=200` to preserve the current iOS transcript depth from
  `sessionEvents(limit: 200)`; backend still caps at 1000.

Acceptance:

- iOS decodes focused session metadata, thread sessions, projection metadata,
  event items, and seam items.
- The API builds the expected `/api/timeline/sessions/{id}/workspace` URL with
  `limit` and `branch_mode`.

### Phase 3: iOS SessionViewModel Migration

- Replace split `sessionDetail` + `sessionEvents` refreshes with one workspace
  load.
- Keep stream and fallback polling, but make both refetch workspace.
- After send and loop-mode updates, refresh workspace instead of fetching raw
  events/detail separately.
- Remove `sessionDetail` and `sessionEvents` from `LonghouseAPI` if no caller
  remains.
- Preserve render-beacon reporting using the latest event from the workspace
  projection.
- Subscribe to the workspace stream with `skip_initial=true` so the bootstrap
  workspace load is not immediately followed by a duplicate refetch.
- Replace `allowPartial` with explicit semantics:
  - initial load failure shows the error screen
  - stream/poll refresh failure keeps the last good state and logs quietly

Acceptance:

- Tapping a timeline card loads session detail through one workspace request.
- Stream wake and polling use the same refresh path.
- Stale session guards still prevent late responses from mutating another
  session.
- `send` and `setLoopMode` both refresh the full workspace.
- Render beacons still fire after new transcript events render.

### Phase 4: Navigation Smoke Coverage

- Add an iOS UI smoke path for the launch-critical flow:
  authenticated timeline -> tap first session -> session detail renders.
- Use an explicit test fixture mode rather than a live hosted dependency.
- Keep hosted-login UI tests separate.

Acceptance:

- The test would have failed for a blank/crashing session detail screen.
- The fixture mode is clearly gated to UI tests/debug builds and cannot run in
  production by accident.

### Phase 5: iOS Seam Rendering

- Render `projection.items` directly instead of dropping seam items before
  `TimelineBuilder`.
- Show continuation/origin markers with the same semantics as web.

Acceptance:

- iOS and web agree about continuation boundaries in a projected session.
- Existing tool pairing and passive grouping behavior is preserved.

## Non-Goals

- A broad iOS DI container.
- Rewriting the full SwiftUI session screen in one pass.
- Making iOS use `/api/agents/*` machine-token auth.
- Implementing full projection pagination on iOS in the workspace migration.
- Rewriting `TimelineBuilder` to consume projection items before Phase 5.
- Redesigning timeline card visual layout.

## Test Plan

- Backend: targeted route/service tests for workspace response parity.
- iOS unit: workspace decode/API URL tests.
- iOS view-model: mocked workspace loader tests covering initial load,
  stream/poll refresh, and stale-session protection.
- iOS UI: fixture-backed timeline tap smoke.
- Local verification:
  - `xcodebuild test -project ios/XcodeHarness/LonghouseIOS.xcodeproj -scheme Longhouse -destination 'platform=iOS Simulator,name=iPhone 17'`
  - `xcodebuild build -project ios/XcodeHarness/LonghouseIOS.xcodeproj -scheme Longhouse -destination 'id=00008150-001244D21A87801C'`

There is no `make` target for iOS; Xcode build/test is the supported loop.
