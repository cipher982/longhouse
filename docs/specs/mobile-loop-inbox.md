# Mobile Loop Inbox

Status: in progress
Owner: David / Longhouse product direction
Updated: 2026-03-19

## Executive Summary

Longhouse should not try to make the full desktop session workspace usable on a phone.

The phone product is a separate, tiny surface for one job:

- a coding session finishes a turn
- Longhouse summarizes what happened
- the phone shows the recommended next action
- the user taps a button instead of opening VNC or typing into a terminal text box

This spec keeps the first mobile slice deliberately narrow:

- one lightweight inbox of sessions that need attention
- one action card per session
- one-tap actions for the common case
- full transcript only as an escape hatch

The goal is not “mobile-responsive Longhouse.” The goal is “away-from-keyboard loop control.”

## Problem

The current friction is not primarily reasoning. The friction is input ergonomics.

Today, when a coding session finishes a turn while the user is away:

- the user has to notice it
- open the right session
- inspect enough context
- find the text input
- type the obvious next response

That is acceptable on a laptop and terrible on a phone.

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

## V1 Scope

### Included

- Dedicated backend/mobile contract for a loop inbox
- Dedicated backend/mobile contract for one session action card
- Session summaries driven by `SessionTurnReview`
- Recommended action + optional follow-up prompt
- One-tap same-session action path
- PWA-specific mobile shell later in a separate phase

### Excluded

- Full mobile transcript/timeline parity
- Rich lock-screen action support
- Broad arbitrary phone chat composition
- General runner management from the phone
- Swift/native app work

## V1 Mobile UX

### Surface 1: Inbox

List only sessions that need attention.

Each row should include:

- session title
- project / machine
- freshness (`2m ago`)
- short summary
- decision badge (`Continue`, `Needs approval`, `Wait`, `Escalate`)

### Surface 2: Action Card

The main phone screen for one session should include:

- session title
- project / machine
- latest turn summary (2–4 lines)
- recommended action
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

### Surface 3: Details

Optional expanded context:

- last user instruction
- last assistant turn
- small metadata like branch / cwd / tests signal

Full transcript remains a secondary escape hatch.

## Data Contract

The mobile inbox should work from a thin purpose-built contract, not by scraping wakeups or desktop pages.

### Inbox item fields

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
- `requires_attention`

### Action card fields

- all inbox item fields
- `last_user_text`
- `last_assistant_text`
- `mode_summary`
- `mode_capability`
- `available_actions`

## Success Criteria

### Product

- The user can keep a session moving from a phone without opening the desktop Longhouse UI in the common case.
- The common mobile interaction path requires taps, not typed text.

### UX

- Inbox loads quickly and shows only sessions that need action.
- Opening one session reveals a compact action card, not the full desktop layout.

### Safety

- The default phone action scope is same-session only.
- Risky or ambiguous turns do not present as blind one-tap continue actions.

### Dogfood

- At least one real traveling / away-from-keyboard workflow works end-to-end:
  - session finishes a turn
  - phone sees it
  - user taps `Continue`
  - same session resumes

## Phases

### Phase 1: Thin Loop Inbox read contract

- add dedicated inbox/card backend endpoints on top of `SessionTurnReview`
- return concise action-ready data only

Done when:

- backend exposes a clean list of sessions needing mobile attention
- backend exposes a single action-card response for one session
- focused tests cover filtering, sorting, and payload shape

### Phase 2: One-tap action contract

- add backend mutation endpoints for `Continue`, `Not now`, and simple approval

Done when:

- same-session continue can be triggered without the full desktop session page
- action endpoints are bounded and test-covered

### Phase 3: Tiny mobile PWA shell

- build a separate phone-first shell for inbox + card + details

Done when:

- the phone surface no longer depends on the desktop session layout
- action card flow works well on iPhone Safari / installed Home Screen app

### Phase 4: Push notifications

- notify when a session finishes a turn that needs attention
- deep-link into the relevant action card

Done when:

- the user does not need to poll the desktop site to know a turn is ready
