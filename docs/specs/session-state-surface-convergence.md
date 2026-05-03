# Session State Surface Convergence

Status: Draft for implementation
Last updated: 2026-05-03
Related:
- `session-runtime-display-contract.md`
- `session-lifecycle-liveness-contract.md`
- `session-liveness-honesty.md`
- `ios-session-workspace-contract.md`

## Executive Summary

Longhouse currently has the right backend direction, but the user-facing
surfaces still mix three different questions:

1. Does Longhouse own the control path? (`managed` / `unmanaged`)
2. What is the best-known current activity state? (`working`, `needs_user`,
   `ready`, `recent`, `stale`, `closed`, etc.)
3. What actions are currently available? (`send`, `queue`, `steer`,
   `reattach`, or read-only)

The goal of this slice is to make those axes explicit and consistent across
backend API responses, web timeline/detail, and iOS timeline/detail. A session
card should never imply live control just because durable metadata says the
session was once managed, and a detail composer should not describe action
availability as if it were the session's state.

## Goals

- Use `runtime_display` as the canonical display-state contract for web and
  iOS when it is present.
- Use `control_path` only for ownership copy: `Managed` or `Unmanaged`.
- Use lifecycle/activity fields only for state copy: `Working`, `Needs you`,
  `Ready`, `Recent`, `Stale`, `Disconnected`, `Closed`, or `Unknown`.
- Use capability booleans only for action enablement and action-specific copy.
- Remove user-facing `Live control` as a state or ownership label.
- Keep the implementation readable and direct. No env flags, no alternate
  behavior branches, and no new client-side state machine.

## Non-Goals

- Do not redesign provider control transports.
- Do not add new runtime host or engine heartbeat semantics in this slice.
- Do not make unmanaged sessions steerable.
- Do not remove compatibility fallbacks for older payloads yet; mark them as
  fallback only and keep them smaller than the server contract.
- Do not introduce a broad schema migration unless implementation proves it is
  required for the client contract.

## Target Contract

Every modern session payload should contain enough information to answer these
questions without client inference:

```text
runtime_display.control_path:     managed | unmanaged
runtime_display.lifecycle:        open | closed | unknown
runtime_display.activity_recency: live | recent | stale | none
runtime_display.host_state:       online | stale | offline | unknown
runtime_display.state:            thinking | running | idle | needs_user | blocked | stalled | null
runtime_display.phase_label:      short state label suitable for a badge
runtime_display.headline:         short human headline
runtime_display.detail:           short human detail

capabilities.can_send_input:      action availability
capabilities.can_queue_input:     action availability
capabilities.can_steer:           action availability
capabilities.host_reattach_available: action availability
```

Durable management and current action availability must not be collapsed. A
session can be `Managed` while current control is offline. A session can be
`Unmanaged` while it has recent transcript activity. A session can be `Closed`
while it was historically managed.

## User-Facing Vocabulary

### Ownership

Ownership answers who owns the control path:

| Value | Label |
| --- | --- |
| `managed` | `Managed` |
| `unmanaged` | `Unmanaged` |

Ownership labels should use neutral visual treatment unless paired with an
explicit current action.

### State

State answers what Longhouse currently believes is happening:

| Backend condition | Preferred label |
| --- | --- |
| `lifecycle=closed` | `Closed` |
| fresh `state=thinking` | `Thinking` |
| fresh `state=running` | `Working` or tool-specific `phase_label` |
| fresh `state=needs_user` | `Needs you` |
| fresh `state=blocked` | `Needs permission` or `Blocked` |
| `state=stalled` | `Stalled` |
| open + idle/live control available | `Ready` |
| open + recent activity | `Recent` |
| open + stale managed control | `Disconnected` |
| open + stale unmanaged activity | `Stale` |
| no usable signal | `Unknown` |

The exact badge label can remain backend-supplied via `phase_label` where it is
already more specific, but clients should not turn action names into state.

### Actions

Action copy belongs near controls, not in the timeline ownership/state row:

| Capability | Copy |
| --- | --- |
| live send available | `Send update` / `Send` |
| queue available | `Queue next` |
| reattach available | `Reattach` |
| no action available on managed session | `Control offline` |
| no action available on unmanaged session | `Read only` |

`Live control` is removed from user-facing state/ownership copy. It can remain
as an internal concept in code only if the surrounding API still uses that
field name.

## Implementation Plan

### Phase 0 - Spec and Review

Acceptance criteria:

- Persistent spec records goals, contract, phases, and test strategy.
- Hatch Opus reviews the plan before implementation starts.
- Spec is committed before code changes.

### Phase 1 - Backend Contract Tightening

Intent: make backend-provided display and capability copy match the three-axis
model before clients consume it more aggressively.

Expected changes:

- Update capability display fallback so it never emits `Live control` as a
  user-facing label.
- Keep `Managed` / `Unmanaged` ownership separate from current action
  availability.
- Add or update backend tests for:
  - managed + live host/action availability
  - managed + stale/offline control path
  - unmanaged + recent activity
  - unmanaged + no current action
  - closed managed session

Acceptance criteria:

- Backend payloads expose stable ownership through `runtime_display.control_path`.
- Capability display labels are action/read-only labels, not state labels.
- Backend tests cover the screenshots' failure cases.

### Phase 2 - Web Surface Alignment

Intent: timeline and detail should render ownership, state, and actions as
separate concepts.

Expected changes:

- Timeline card ownership pill uses `runtime_display.control_path` and neutral
  ownership styling.
- Timeline state badge uses server `runtime_display` state/copy first and
  avoids capability-derived labels.
- Session workspace/detail interaction model derives:
  - ownership from `runtime_display.control_path`
  - state from `runtime_display`
  - action enablement from `capabilities`
- Composer disabled/help copy stops saying `Live control is unavailable`.

Acceptance criteria:

- Web no longer shows `Live control` as a session state or ownership concept.
- A managed but offline/stale session renders as managed ownership plus offline
  action state, not as live.
- An unmanaged recent/running-looking session remains read-only unless current
  capabilities say otherwise.
- Frontend tests cover interaction derivation and card labels.

### Phase 3 - iOS Surface Alignment

Intent: native iOS should use the same contract and vocabulary as web without
duplicating a larger state machine.

Expected changes:

- Swift models expose small derived helpers for ownership, state, and action
  labels from backend payloads.
- Timeline card uses `Managed` / `Unmanaged` for ownership and state badges for
  activity/lifecycle only.
- Session detail/composer removes `Live control` and `Reattach` as ownership
  labels. Reattach remains an action when available.
- Add Swift tests for managed offline, unmanaged recent, closed, and live
  managed cases.

Acceptance criteria:

- iOS and web agree on the same session examples.
- iOS keeps optional decoding compatibility for older hosted payloads.
- Xcode targeted model tests pass.

### Phase 4 - Integration and Regression Testing

Intent: prove the full path works and prevent regressions.

Expected checks:

- `make test`
- `make test-frontend`
- iOS `xcodebuild ... -only-testing:LonghouseIOSTests/SessionModelsTests`
- Add narrower targeted tests if failures reveal an uncovered contract gap.
- Run broader checks if backend/web contract edits have cross-surface blast
  radius.

Acceptance criteria:

- Unit and integration tests pass for touched layers.
- No stale `Live control` user-facing copy remains in web or iOS state surfaces.
- Commits are atomic by phase.

## Decision Log

### Decision: Treat Live Control as an Action, Not a State

Context: A session displayed `Needs you` and `Live control` even though the
local process had exited. The phrase implied Longhouse was actively connected
to a running process.

Choice: Remove `Live control` from state and ownership UI. Action controls may
still be enabled when current capability projection says a live send/steer path
exists.

Rationale: Users need to understand ownership and state first. Action
availability is derivative and can change independently.

Revisit if: Longhouse introduces a dedicated live transport indicator with a
strong heartbeat-backed guarantee and a narrowly scoped control location.

### Decision: Keep Contract Simple for This Slice

Context: The deeper model includes host expiry, process-gone promotion, and
managed detachment semantics.

Choice: This slice aligns existing display/capability contracts and clients. It
does not add new heartbeat semantics or storage migrations.

Rationale: The screenshots show a presentation and projection bug in current
surfaces. Fix that first, then extend raw truth only if remaining examples still
cannot be represented honestly.

Revisit if: Testing shows the backend cannot represent a common state without a
new raw signal.
