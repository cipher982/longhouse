# Mobile Loop Inbox

Status: in progress
Owner: David / Longhouse product direction
Updated: 2026-03-21

## Executive Summary

Longhouse should not try to make the desktop session workspace usable on a phone.

The phone product is a separate, tiny surface for one job:

- a coding session finishes a turn
- Longhouse creates a follow-up card for that exact turn
- the phone shows the recommended next action
- the user taps a button instead of opening VNC or typing into a terminal text box

The goal is not “mobile-responsive Longhouse.” The goal is “away-from-keyboard loop control.”

The key pivot in this spec is:

- `/loop` must be reachable from the main authenticated app without a deep link
- Telegram is not the approval surface
- `/loop` is the canonical mobile app
- notifications must point at a stable follow-up card, not at a session-level inbox row that can disappear
- installed Loop should receive web push directly when available; Telegram is fallback only

## Problem

The current friction is not primarily reasoning. The friction is input ergonomics.

Today, when a coding session finishes a turn while the user is away:

- the user has to notice it
- open the right session
- inspect enough context
- find the text input
- type the obvious next response

That is acceptable on a laptop and terrible on a phone.

The current Telegram-based notification path also has a product bug:

- notifications are durable chat messages
- links point at `/loop/{session_id}`
- `/loop` only keeps the latest actionable item per session
- old notifications go stale and can fall into empty state / 404 behavior

That makes Telegram a poor approval surface.

## Product Principles

### 1. Buttons beat chat boxes on mobile

The default mobile interaction should be tapping:

- `Continue`
- `Not now`
- `Open details`

Freeform text is an escape hatch, not the main path.

### 2. Derived context beats raw transcript

Phone surfaces should show:

- what just happened
- what Longhouse recommends
- what action is available

They should not default to hundreds of transcript messages.

### 3. Desktop and mobile have different jobs

- Desktop web app: full session workspace, timeline, transcript, debugging
- Mobile Loop Inbox: concise attention queue and action cards

### 4. Assist comes before broad autopilot

The most important initial win is a lightweight approve/deny path.

Full autonomy should stay bounded and rare until the phone flow is proven.

### 5. Notifications are separate from approvals

Telegram, web push, or any future nudge channel should only tell the user that a turn needs attention.

The actual approval path should live in `/loop`, where actions are scoped to one exact follow-up card.

### 6. Discoverability matters more than purity

`/loop` can remain a dedicated mobile surface without being hidden.

Users should be able to reach it from the normal authenticated app, even on desktop, because:

- it reduces product confusion
- it makes setup and dogfooding much faster
- it avoids requiring an old notification or memorized URL

### 6. One completed turn equals one follow-up card

The unit of phone action is not “a session.”

It is one exact assistant turn that produced a recommended next step.

That follow-up card should remain inspectable even after it becomes stale, acted, or superseded.

## User Stories

### 1. Approve the obvious next step

When a session finishes a turn that clearly suggests a next step, I want my phone to show:

- a short summary
- the recommended action
- a `Continue` button

so I can keep the session moving without opening the desktop UI.

### 2. Make a small structured choice

When a session needs a real but simple decision, I want the phone to show:

- a short summary
- 2–3 structured options

so I can choose without typing into a terminal.

### 3. Escalate risky or ambiguous work

When the session needs real inspection, I want the phone to show:

- a short summary
- `Review`
- `Not now`

so risky work does not get flattened into fake convenience.

### 4. Open an old notification and still get a sensible result

When I tap a Telegram or push notification hours later, I want the mobile app to explain whether that follow-up card is:

- still active
- already handled
- superseded by a newer turn

instead of dropping me into a dead link.

## V1 Scope

### Included

- Clear entry point to `/loop` from the authenticated app
- Dedicated backend/mobile contract for a loop inbox
- Dedicated backend/mobile contract for one follow-up card
- Session summaries driven by `SessionTurnReview`
- Recommended action + optional follow-up prompt
- One-tap same-session action path
- Lightweight phone shell at `/loop`
- Web push registration for installed Loop
- Web push as the primary notification path when available
- Telegram as optional notification/fallback only

### Excluded

- Full mobile transcript/timeline parity
- Rich lock-screen action support
- Broad arbitrary phone chat composition
- General runner management from the phone
- Native iOS work
- Using Telegram chat replies as the canonical approval flow

## V1 Mobile UX

### Surface 1: Inbox

List only active follow-up cards that need attention.

Each row should include:

- session title
- project / machine
- freshness (`2m ago`)
- short summary
- decision badge (`Continue`, `Needs approval`, `Wait`, `Escalate`)

### Surface 2: Action Card

The main phone screen for one follow-up card should include:

- session title
- project / machine
- latest turn summary (2–4 lines)
- recommended action
- explicit card status
- buttons

Default button patterns:

- obvious case:
  - `Continue`
  - `Not now`
  - `Details`
- choice case:
  - `Option A`
  - `Option B`
  - `Details`
- risky case:
  - `Review`
  - `Not now`

### Current UI pass: phone queue sheet

For the current frontend pass, keep the existing split inbox + card layout on desktop/tablet and change only the phone presentation:

- phone uses a content-first layout where the selected card is visible immediately
- the attention queue is hidden by default behind a compact queue toggle
- the queue opens in an accessible modal bottom sheet instead of sitting inline above the card
- selecting a queue item closes the sheet and swaps the active card
- if there is only one active follow-up, the queue control stays hidden

Implementation guardrails:

- no backend/API contract changes
- no redesign of the desktop/tablet split view
- use a content-driven phone breakpoint aligned with the app's mobile nav threshold (`<768px`)
- preserve direct deep-linking to `/loop/card/{id}` so notification-opened cards remain above the fold

### Surface 3: Details

Optional expanded context:

- last user instruction
- last assistant turn
- small metadata like branch / cwd / tests signal

Full transcript remains a secondary escape hatch.

### Notification Surface

Notifications should be short and ephemeral:

- title
- short summary
- one deep link into the exact follow-up card

Notifications should not:

- carry the full approval UX
- rely on free-text replies
- open stale or disappearing URLs
- show noisy link previews

Primary notification channel order:

1. installed Loop PWA via web push
2. Telegram fallback when no active Loop push subscription exists

## Data Contract

The mobile inbox should work from a thin purpose-built contract, not by scraping wakeups or desktop pages.

### Follow-up card identity

Each mobile item must have a stable id that points to one exact reviewed turn.

V1 may reuse `SessionTurnReview.id` as the card id instead of introducing a separate table.

### Inbox item fields

- `card_id`
- `session_id`
- `title`
- `project`
- `machine`
- `provider`
- `loop_mode`
- `decision`
- `execution_state`
- `summary`
- `recommended_action`
- `follow_up_prompt`
- `blocked_reasons`
- `last_turn_at`
- `card_state`
- `requires_attention`

### Action card fields

- all inbox item fields
- `last_user_text`
- `last_assistant_text`
- `mode_summary`
- `mode_capability`
- `available_actions`
- `superseded_by_card_id`
- `card_state_reason`

### Card states

Each card should resolve to one of:

- `active`
- `acted`
- `dismissed`
- `superseded`
- `expired`
- `failed`

Old links should keep working even when a card is no longer active.

If a card is superseded, the phone UI should explain that and offer `Open latest`.

## Success Criteria

### Product

- The user can keep a session moving from a phone without opening the desktop Longhouse UI in the common case.
- The common mobile interaction path requires taps, not typed text.
- `/loop` is reachable from the authenticated app without relying on a prior notification.

### UX

- Inbox loads quickly and shows only active cards that need action.
- Opening one card reveals a compact action card, not the full desktop layout.
- Notification links never drop the user onto a 404 or empty state without explanation.
- Installed Loop can request push permission and register without requiring desktop-only setup.

### Safety

- The default phone action scope is same-session only.
- Risky or ambiguous turns do not present as blind one-tap continue actions.
- Telegram free-text replies are not required for the core flow.

### Dogfood

- At least one real traveling / away-from-keyboard workflow works end-to-end:
  - session finishes a turn
  - phone sees a Loop notification
  - user opens the exact card in `/loop`
  - user taps `Continue`
  - same session resumes

## Phases

### Phase 1: Stable follow-up card identity

- reuse `SessionTurnReview.id` as `card_id`
- move deep links from session ids to card ids
- keep old card links resolvable with explicit stale/superseded state
- stop Telegram from showing noisy previews

Done when:

- backend exposes inbox/card/action APIs keyed by `card_id`
- `/loop` can open a stale or superseded card without 404
- Telegram links target exact cards

### Phase 2: PWA-first mobile shell

- make `/loop` card-centric instead of session-centric
- keep the shell tiny and fast
- optimize for installable phone use, not desktop parity
- add an obvious entry point from the authenticated app

Done when:

- the phone surface feels like a dedicated action app
- action card flow works well on iPhone Safari / installed Home Screen app
- a logged-in user can reach `/loop` from the normal app nav

### Phase 3: Real phone nudges

- register installed Loop for web push
- send web push nudges into `/loop` when subscriptions exist
- keep Telegram as fallback only when no Loop subscription exists or push delivery fails hard

Done when:

- the user does not need to poll the desktop site to know a turn is ready
- the approval surface is `/loop`, not Telegram chat
- an installed Loop PWA receives the nudge before Telegram in the common case
