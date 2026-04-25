# iOS Attention Surfaces

Status: Active
Owner: iOS companion
Updated: 2026-04-25

## Goal

Make Longhouse iOS feel like a mobile Timeline and pager for active agent work,
not an email inbox. Each iOS surface has one job:

- Visible notification: one session is blocked on the user.
- Widget: what is live or needs attention now.
- Live Activity: the user explicitly chose to watch one session.
- App: the full timeline, detail, search, and reply surface.

## Problem

The first iOS app shipped as a basic recent-session inbox. It lagged behind the
web Timeline and the macOS menu bar because widgets and background app work are
not realtime on iOS. Treating the widget as a full timeline makes the failure
mode worse: stale historical rows look like missing product truth.

The product needs explicit attention semantics that fit iOS constraints:

- WidgetKit timelines are snapshots and reloads are system-budgeted.
- Background pushes can wake the app opportunistically, but delivery is not
  guaranteed.
- WidgetKit push notifications can refresh widgets on iOS 26+, but are still
  budgeted and require widget push-token registration.
- Live Activities are the lock-screen surface for realtime-ish status, but they
  must be explicit, bounded, and session-specific.

## Surface Contracts

### Visible APNs Alerts

Visible alerts fire only when a session enters an attention state:

- `needs_user`
- `blocked`

Alerts should collapse by session, open directly into that session, and clear
when the session no longer needs attention if iOS gives the app background time.

### Widget

The widget is not a history view. It shows:

1. Sessions needing attention.
2. Then most-recent active sessions.

The widget may show a cached snapshot when iOS does not grant a fresh reload,
but the app must refresh the cache whenever it foregrounds, receives a push, or
loads the Timeline.

### Live Activity

Live Activity support is opt-in per session through a future `Watch session`
action. It is never automatic for every active session and never a replacement
for the widget.

### App Timeline

The app remains the full-fidelity surface. Opening the app always fetches fresh
server state, reconciles delivered attention notifications, and refreshes the
shared widget snapshot.

## Phases

### Phase 1: Attention Alerts

Completed in `ios-timeline-refresh`.

- Rename app surface from Inbox to Timeline.
- Send visible APNs only on attention transitions.
- Deep-link alert taps into the session.
- Keep notification copy short and semantic.
- Debounce repeated attention alerts by session/state.

### Phase 2A: Cleanup And Shared Widget Snapshot

Completed in `ios-timeline-refresh`.

- Add a background push on attention resolution.
- In the app background push handler:
  - fetch the current active/attention session set
  - update an App Group widget snapshot
  - remove delivered attention alerts for sessions no longer needing attention
  - request a WidgetKit reload
- Make widget ordering deterministic: attention first, then recent active.
- Keep existing widget network fetches, but use the shared snapshot as fallback
  when fetch/auth/background budgets fail.

### Phase 2B: WidgetKit Push For iOS 26+

Build after Phase 2A is stable.

- Register WidgetKit push tokens from the widget extension.
- Send widget pushes when the attention/active set changes.
- Debounce server-side by set-level transitions, not by event or tool call.
- Preserve timeline-based fallback for older iOS and budget exhaustion.

### Phase 3: Explicit Watch Session

Build after widget freshness is stable.

- Add `Watch session` from session detail and, optionally, the widget.
- Start one Live Activity for that session.
- Update/end via ActivityKit pushes on phase transitions.
- Handle activity duration limits by ending cleanly or requiring explicit user
  renewal.

## Non-Goals

- Realtime widget streaming.
- Showing all historical sessions in the widget.
- Automatically starting a Live Activity for every active session.
- Adding a Notification Service Extension until there is a concrete need for
  encrypted payloads, media, or pre-display rich content.

## Acceptance Criteria For Phase 2A

- When a session resolves from `needs_user` or `blocked`, the server sends a
  silent attention-resolution push to registered iOS devices.
- The iOS app handles the silent push without navigating.
- Silent-push handling refreshes the active session snapshot, removes resolved
  delivered attention alerts, and asks WidgetKit to reload.
- The widget uses the shared snapshot when a fresh fetch cannot complete.
- Widget rows are ordered by attention first, then most-recent active.
- Backend and iOS tests cover the new behavior.
