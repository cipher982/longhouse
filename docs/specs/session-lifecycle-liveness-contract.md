# Session Lifecycle and Liveness Contract

Status: Building
Last updated: 2026-05-20
Related: `session-liveness-honesty.md`, `session-runtime-display-contract.md`, `machine-local-managed-session-state.md`, `managed-codex-liveness.md`

## Purpose

Longhouse must not confuse "the last phase signal is old" with "the
provider session is gone" or "the managed control path is offline." Timeline,
iOS, widgets, and agent-facing APIs should all derive session display and
control affordances from the same small set of axes.

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

7. Control liveness has its own clock.
   A managed control lease refresh proves the control path was seen recently.
   It does not prove provider progress, and an old provider phase does not prove
   the control path is offline.

## Raw Signals

| Signal | Producer | Meaning |
| --- | --- | --- |
| `phase_signal` | Hook outbox, bridge, runtime event ingest | Last known provider activity phase. |
| `terminal_signal` | Managed bridge/control path, explicit close | Durable session end. |
| `unmanaged_session_bindings` snapshot | Machine Agent heartbeat | Full set of live unmanaged process/transcript bindings on that machine. |
| `managed_control_lease` | Machine Agent heartbeat, Machine Agent control channel | Freshness, state, transport, and capability of a managed control path. |
| `managed_sessions` snapshot | Machine Agent heartbeat | Full set of managed control paths/process leases known on that machine. This is the current carrier for `managed_control_lease` facts. |
| host heartbeat | Machine Agent heartbeat | Machine reachability and snapshot freshness. |

## Wire Ownership

The transport lanes are part of the contract. Do not move a fact to a different
lane just because another lane is convenient.

| Lane | Protocol | Producer/consumer | Owns | Does not own |
| --- | --- | --- | --- | --- |
| Durable history | HTTP batch POST | Machine Agent -> Runtime Host | Transcript, tool, and archive events that must spool and replay. | Whether a session can be controlled right now. |
| Provider runtime | HTTP batch POST | Hook/bridge/wrapper -> Runtime Host | Provider phase and explicit terminal signals. | Managed control lease freshness. |
| Machine heartbeat | HTTP POST | Machine Agent -> Runtime Host | Host reachability, shipper health, process snapshots, managed lease snapshots. | User commands or UI streaming. |
| Machine control | Outbound WebSocket | Machine Agent <-> Runtime Host | Live command delivery, command ACK/failure, control capabilities, and attached lease freshness. | Durable transcript history. |
| User command | HTTP POST | Browser/iOS -> Runtime Host | Authenticated user intent such as send, interrupt, steer. | Direct laptop control. |
| UI observation | SSE/fetch | Runtime Host -> Browser/iOS | Foreground timeline/session updates after committed state changes. | Product truth or command transport. |
| Mobile wake | APNS | Runtime Host -> iOS | Background attention/completion wakeups. | Foreground control or durable history. |

## Derived Axes

Every session response should expose these axes through `runtime_display`:

```text
control_path:     managed | unmanaged
control_state:    online | degraded | offline | unknown | none
lifecycle:        open | closed
activity_recency: live | recent | stale | none
host_state:       online | stale | offline | unknown
terminal_reason:  provider_signal | process_gone | user_closed | host_expired | null
state:            thinking | running | idle | needs_user | blocked | stalled | null
```

`state` is provider phase. `lifecycle` is existence. `activity_recency` is
transcript/provider activity freshness. `host_state` is machine reachability.
`control_state` is whether the managed control path can currently accept
commands. These must not be collapsed into one status.

Control affordances derive from the control axis:

```text
can_send =
  lifecycle == open
  and control_path == managed
  and control_state == online
```

Phase is not part of this predicate. A session can be `state=idle` with an old
phase timestamp and still be sendable when the managed control lease is fresh.
A session can have recent transcript activity and still be read-only when the
managed control lease is stale.

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

## Current Implementation Gap

As of 2026-05-20, the code has the right transport pieces but not the final
ownership boundary:

- The Rust Machine Agent ships transcript history through
  `POST /api/agents/ingest`.
- Runtime/provider facts arrive through
  `POST /api/agents/runtime/events/batch`.
- Machine reachability and lease snapshots arrive through
  `POST /api/agents/heartbeat`.
- The Machine Agent exposes `GET /api/agents/control/ws` for live control.
- Browser and iOS commands enter through `POST /api/sessions/{id}/send-live`,
  `POST /api/sessions/{id}/interrupt-live`, and
  `POST /api/sessions/{id}/input`.
- Agent-facing command calls also use
  `POST /api/agents/sessions/{id}/send-live` and
  `POST /api/agents/sessions/{id}/interrupt-live`.
- Web and iOS observe foreground session changes through
  `GET /api/timeline/sessions/stream` and
  `GET /api/timeline/sessions/{id}/workspace/stream`.
- Managed lease truth currently arrives in the heartbeat `managed_sessions`
  payload, then the server converts it into `phase_signal` events with source
  `engine_attached_lease`.
- Capability projection still treats a current phase/runtime observation as
  evidence that managed control is available.

That last two-step is the boundary to remove. Managed lease freshness should be
materialized as a control fact, not as a provider phase fact. The phase reducer
should describe what the provider is doing; the control lease should decide
whether the composer can send.

## Success Criteria

This cleanup is complete when all of these are true:

- Managed control availability is represented as an explicit fact/axis with
  its own freshness clock.
- `live_control_available`, composer enablement, send-live, interrupt-live,
  and `/api/agents/*` capabilities no longer depend on provider phase freshness
  or transcript recency.
- Stale provider phase plus fresh managed control lease keeps control available.
- Recent transcript/progress plus stale managed control lease does not make
  control available.
- Detached/degraded managed control is shown as control unavailable without
  closing the session lifecycle.
- Provider phase ordering remains about provider activity only.
- Web and iOS consume the same server-side control contract instead of
  re-deriving control availability from stale phase/status strings.
- The compatibility path that turns managed leases into provider phase signals
  is removed or explicitly fenced as legacy-only with tests proving it no
  longer drives control availability.

## Implementation Phases

1. Lock the contract in tests.
   Cover open+needs_user, closed+needs_user, stale activity, process gone,
   host offline, managed detached, explicit terminal cases, stale phase with
   fresh control lease, and recent transcript with stale control lease.

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

5. Add explicit managed control facts.
   Introduce a `control`/`control_state` projection sourced from managed lease
   snapshots and the Machine Agent control channel. Keep compatibility with the
   current lease-derived runtime events until every caller consumes the new
   control fact.

6. Move capability projection to control facts.
   `live_control_available`, composer enablement, send-live, interrupt-live,
   and agent-facing capabilities should gate on lifecycle + structural managed
   control path + fresh control lease. They should not gate on phase freshness
   or transcript recency.

7. Retire lease-as-phase compatibility.
   Once capability and client projections consume explicit control facts, stop
   generating provider `phase_signal` events merely to keep managed control
   alive. Keep provider phase ordering solely for provider activity display.

8. Verify client parity.
   Web and iOS must consume `runtime_display.lifecycle`, `state`,
   `host_state`, `control_state`, and `terminal_reason` rather than
   re-deriving closure or control availability from `ended_at`, stale phase, or
   status strings.

## Development Gate

Every future liveness/control change must answer these questions before code
lands:

1. Which product question does this fact answer: lifecycle, control,
   provider phase, transcript activity, host reachability, or user attention?
2. Which wire lane owns it?
3. What is the authoritative clock for freshness?
4. What persists so the state can be rebuilt after restart?
5. Which projection field will web, iOS, and `/api/agents/*` consume?
6. What positive and negative product-level regression prove the boundary?

If a change needs one timestamp or reducer branch to answer more than one of
those questions, split the fact before patching the edge case.
