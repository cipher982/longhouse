# iOS Loop Surface MVP

Status: Active MVP
Last updated: 2026-04-14

## Goal

Restart the iPhone home-screen Longhouse surface with the smallest honest MVP that matches the launch story.

The MVP should let a user pull out their phone, open Longhouse from the home screen, see which sessions are waiting on them, and take a lightweight next action without opening the full desktop workspace.

## Platform Reality

### What web can do now

- A home-screen web app on iPhone is a real product surface again, not just a bookmark.
- Manifest metadata, icons, standalone display, service workers, and notification plumbing still matter for polish and resilience.
- iPhone install UX still needs explicit product copy and in-app hints. We should not assume a Chromium-style install prompt.

### What web cannot do yet

- A true iOS Home Screen widget is still a native surface.
- That means any real widget path needs an iOS app target plus a widget extension, not just the existing PWA.
- Shared data for that widget will need an app-owned bridge such as App Groups, not direct PWA-only state.

## Product Decision

### Near-term MVP

Ship `Loop` as a focused home-screen web app at `/loop`.

This MVP is:

- mobile-first
- authenticated
- fast to reopen from the iPhone home screen
- built on current Longhouse session primitives
- explicit about what the phone can do right now

This MVP is **not**:

- a resurrection of the deleted Oikos operator subsystem
- an autonomy review queue
- a fake native widget story

### User promise

When a session is waiting on the user, `/loop` should make it obvious and actionable:

- show the sessions that currently need attention
- show enough recent context to decide quickly
- allow lightweight defer/snooze
- allow direct reply into live managed-local sessions when supported
- provide one tap back to the full timeline/session view

## Canonical Data Model

The Loop MVP should derive from current session truth, not from a separate review pipeline.

Primary signals:

- `presence_state` of `needs_user` or `blocked`
- `user_state` for user-driven hiding/snoozing
- session preview messages from the timeline/session surfaces
- `reply_to_live_session_available` / managed-local live-send capability
- `home_label`, provider, project, machine, and loop mode for compact context

## Scope

### In scope

- `/loop` standalone route
- iPhone-first page chrome
- install hint for iOS home-screen add flow
- focused queue of attention-needing sessions
- session detail card with recent context
- `Not now` mapped to existing user-state controls
- live reply for supported managed-local sessions
- offline shell/service-worker support for the Loop route

### Out of scope

- WidgetKit widget
- Live Activities
- push-notification reintroduction
- background autonomy/review orchestration
- brand-new backend autonomy models

## Native Follow-On

If we decide to ship a true iOS widget after the PWA proves useful, the native stack should be:

- Xcode + SwiftUI app shell
- WidgetKit extension
- App Intents for widget configuration/actions
- App Group shared state between app and widget
- optional WKWebView shell if we want the main app to wrap the existing Longhouse web surface

That is a separate track from this MVP and should not block the PWA restart.

## Implementation Rules

- Reuse current browser-auth Longhouse session APIs where possible.
- Do not reintroduce deleted Oikos-specific backend concepts.
- Keep `/loop` as a thin, obvious veneer over current session state.
- Prefer a fast local loop: frontend tests first, then local browser QA on the actual route.

## Local QA Loop

For local Vite/dev QA:

- use `make dev`
- open `/loop` for normal UI iteration
- open `/loop?loopsw=1` when you need the real Loop service worker active in local dev
- open `/loop?loopsw=0` to explicitly unregister that Loop-scoped worker and get back to a clean non-PWA dev state

That keeps the default dev loop safe from accidental cache fights while still giving us an honest PWA path for install/offline/home-screen verification.

## Success Criteria

This MVP is good enough when:

- `/loop` works as a real iPhone home-screen surface
- opening it shows only sessions that actually need user attention
- the page is legible and efficient on narrow mobile viewports
- a managed-local session can receive a short reply from the Loop surface
- the user can defer a session and get back to the main timeline in one tap
- the implementation does not drag Oikos operator code back into the repo
