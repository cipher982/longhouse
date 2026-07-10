# Managed Idle Timeline Status

Status: Proposed fix
Last updated: 2026-07-09

## Problem

An attached Helm session can remain locally controllable while its provider has
not emitted a phase or transcript event for more than ten minutes. The Runtime
Host correctly keeps the managed control lease fresh, but the timeline card only
uses provider-runtime freshness when choosing its status label. It therefore
renders an attached, steerable session as `No live signal`; iOS then appends
`stale` because the row is quiet and provider activity is no longer fresh.

That presentation is false. The provider may be idle, but Longhouse still has a
live control path to the real terminal session.

## Incident Evidence

At 2026-07-09 19:08 MDT, the hosted heartbeat for device `cinder` reported three
sessions with `control_path=managed` and `state=attached`:

- Claude `5bc48e4d-2d8b-455f-84c5-3f3c1198cfb0`, phase `needs_user`
- Codex `e58efb7a-20de-4832-a716-4e7c2d1c2be8`, phase `idle`
- Codex `f303ff2b-e91c-4699-a616-dea27282fdc1`, phase `idle`

The first two appeared in iOS at the same minute as `No live signal · stale`.
Their provider phase timestamps were older than the ten-minute runtime freshness
window, but their control leases and local provider processes were current.

The mismatch began after `71f62639a` correctly stopped converting managed lease
heartbeats into synthetic provider phase events. Control freshness now lives in
the managed connection projection, while the timeline status mapper still falls
back to `No live signal` whenever the real provider phase expires.

## Product Contract

Control liveness and provider activity are separate axes:

- Fresh provider execution: `Thinking`, `Using <tool>`, or the current attention
  state.
- Fresh provider idle phase: `Idle`.
- Fresh send-capable managed control with no fresh provider phase: `Ready`,
  with detail `Live control connected`.
- No live control, process binding, or fresh phase: `No live signal`.
- Explicit terminal truth: `Closed`.

`Ready` means Longhouse can still send to the session. It does not claim the
provider is currently working, recently active, or sitting at a particular TUI
prompt.

The iOS `stale` annotation means the displayed status lacks a current live
signal. It must not be inferred merely because a row is quiet or because
provider activity is old. A quiet `Ready` row is healthy and should not pulse or
show `stale`.

## Implementation

1. In the server-owned timeline-card mapper, preserve the existing precedence
   for transcript sync, pause requests, closed sessions, and fresh provider
   phases. Before the `No live signal` fallback, map the existing managed
   runtime-display `Ready` state to a quiet `Ready` status. The predicate must
   require managed control, open lifecycle, no current provider state, idle
   tone, and the canonical `Ready` phase label; it must not match headline copy
   alone.
2. In iOS, only append `stale` to quiet rows whose server-owned timeline status
   tone is inactive. Do not derive staleness from activity recency alone. Keep
   this predicate outside the SwiftUI view body so it is directly testable.
3. Do not restore synthetic `engine_attached_lease` phase events or extend phase
   freshness from control heartbeats.
4. A `Ready` status omits `seen_at`; the last provider-activity timestamp is not
   the observation time for the current control lease and must not be presented
   as though it were.

## Verification

- Server unit: managed, live-control-ready, stale provider phase maps to
  `Ready` with quiet/idle tone.
- Server negatives: non-sendable live control, unavailable host, and
  reattach-only control remain `No live signal`.
- Server precedence: pending pause, closed, fresh phase, and transcript sync
  continue to outrank `Ready`.
- Server endpoint: a fresh managed control lease plus expired runtime phase
  returns `timeline_card.status.label=Ready`; an expired lease returns
  `No live signal`.
- iOS unit: a quiet `Ready` row does not show `stale`; a quiet
  `No live signal` row still does. Closed rows remain unstale, and legacy
  payloads without a timeline card retain the inactive/stale fallback.
- Existing active, attention, closed, imported, and process-binding cases retain
  their current labels.

## Non-goals

- Changing wrapper, bridge, heartbeat, or lease mechanics.
- Treating reattach-only sessions as live-control-ready.
- Making idle sessions animate as working.
- Reclassifying imported OpenCode or other Shadow sessions as managed.
