# Session Lifecycle and Liveness Contract

Status: Building
Last updated: 2026-04-29
Related: `session-liveness-honesty.md`, `session-runtime-display-contract.md`, `machine-local-managed-session-state.md`, `managed-codex-liveness.md`

## Purpose

Longhouse must not confuse "the last phase signal is old" with "the
provider session is gone." Timeline, iOS, widgets, and agent-facing APIs
should all derive session display from the same small set of axes.

This contract covers agent CLI sessions only. It is intentionally about
raw signals and deterministic projection, not UI copy.

## Axioms

1. Phase is not lifecycle.
   `thinking`, `running`, `idle`, `needs_user`, and `blocked` describe the
   current or last known activity of a session. They do not prove that a
   session is open or closed.

2. Freshness is not completion.
   A stale phase means the last activity signal is too old to trust as live.
   It does not mean the work is done.

3. Attention requires open lifecycle.
   A session needs user attention only when `lifecycle=open` and the current
   semantic phase is `needs_user` or `blocked`.

4. Absence only matters inside a full snapshot.
   A missing heartbeat means the host is unknown/offline. A heartbeat that
   explicitly reports an empty full snapshot means "the machine looked and
   saw nothing."

5. Managed and unmanaged sessions have different closure contracts.
   Managed sessions have a Longhouse-owned control path and may be detached
   or degraded without being done. Unmanaged sessions are observation-only,
   so confirmed process disappearance closes the session.

6. Host offline is not process gone.
   If a machine stops reporting, Longhouse cannot verify process existence.
   That is `host_state=offline` or `unknown`, not `terminal_reason=process_gone`.

## Raw Signals

| Signal | Producer | Meaning |
| --- | --- | --- |
| `phase_signal` | Hook outbox, bridge, managed lease | Last known activity phase. |
| `terminal_signal` | Managed bridge/control path, explicit close | Durable session end. |
| `unmanaged_session_bindings` snapshot | Machine Agent heartbeat | Full set of live unmanaged process/transcript bindings on that machine. |
| `managed_sessions` snapshot | Machine Agent heartbeat | Full set of managed control paths/process leases known on that machine. |
| host heartbeat | Machine Agent heartbeat | Machine reachability and snapshot freshness. |

## Derived Axes

Every session response should expose these axes through `runtime_display`:

```text
control_path:     managed | unmanaged
lifecycle:        open | closed
activity_recency: live | recent | stale | none
host_state:       online | stale | offline | unknown
terminal_reason:  provider_signal | process_gone | user_closed | host_expired | null
state:            thinking | running | idle | needs_user | blocked | stalled | null
```

`state` is phase. `lifecycle` is existence. `activity_recency` is freshness.
`host_state` is machine reachability. These must not be collapsed into one
status.

## Closure Rules

### Managed

Managed means Longhouse owns the control path, not the provider binary.

- `terminal_signal(session_ended)` closes immediately with
  `terminal_reason=provider_signal`.
- `managed_sessions` reports `attached`: lifecycle remains open.
- `managed_sessions` reports `degraded`: lifecycle remains open; control
  path is unhealthy.
- `managed_sessions` reports `detached`: lifecycle remains open during the
  reattach window.
- A managed session missing from a full managed snapshot is not immediately
  closed. It becomes detached/unknown first, then closes only after a bounded
  reattach window if the host remains online and no terminal/control signal
  reappears.
- Provider hook `Stop` is not terminal. It can mean "assistant turn stopped."

### Unmanaged

Unmanaged means Longhouse observes transcript/process data but has no control
path.

- `unmanaged_session_bindings` includes a binding: lifecycle remains open and
  host/process truth is alive.
- A later full unmanaged snapshot omits a previously observed binding from
  the same device: mark the binding stale and close with
  `terminal_reason=process_gone`.
- If the host stops heartbeating, do not close as `process_gone`. Surface
  `host_state=offline` or `unknown`.
- A long host-offline interval may become `terminal_reason=host_expired` only
  if the product explicitly chooses to archive unverifiable old sessions. That
  must be distinguishable from process death.

## Attention Rules

```text
needs_attention =
  lifecycle == open
  and state in {needs_user, blocked}
  and user_state == active
```

Phase freshness is not part of this predicate. Freshness affects sorting and
copy such as "stale", not whether the session semantically asked for input.

## Implementation Phases

1. Lock the contract in tests.
   Cover open+needs_user, closed+needs_user, stale activity, process gone,
   host offline, managed detached, and explicit terminal cases.

2. Complete unmanaged closure.
   Full unmanaged heartbeat snapshots must stale missing bindings, close those
   sessions as `process_gone`, and preserve old-engine compatibility when the
   snapshot field is absent.

3. Add host-expiry semantics.
   Distinguish "machine offline too long" from "process gone" in both backend
   projection and client-visible `terminal_reason`.

4. Add managed disappearance semantics.
   Treat managed detachment/missing managed leases as recoverable first. Close
   only after a bounded reattach window, while preserving explicit terminal
   signals as final.

5. Verify client parity.
   Web and iOS must consume `runtime_display.lifecycle`, `state`,
   `host_state`, and `terminal_reason` rather than re-deriving closure from
   `ended_at`, stale phase, or status strings.
