# Managed Codex Liveness

Status: Active buildout
Owner: local runtime + Runtime Host
Updated: 2026-04-26

## Goal

Managed Codex sessions must show the state of the Longhouse-owned control path,
not the age of the last transcript event.

The launch contract is simple: if the machine still owns an attached managed
Codex control path, the hosted timeline must keep that session visibly live
even when the provider has been idle for a long time.

## Problem

The current hosted timeline can dim an attached managed Codex session as
"State unavailable" after the runtime freshness window expires. That is wrong
for a managed session because Longhouse owns more truth than a transcript tail.

The specific failure mode is:

- Codex transcript ingest treats the newest transcript timestamp as
  `sessions.ended_at`
- bridge/runtime signals refresh active work but do not keep an idle attached
  control path leased
- the runtime reducer can treat stale state plus `ended_at` as completed or
  unavailable even when the local bridge and TUI process are still alive

## Decision

Use two different truth streams and keep their responsibilities separate.

1. **Transcript/archive truth**
   - provider transcript rows and parsed event timestamps
   - updates `last_activity_at`
   - never ends a managed Codex control path
2. **Managed control-path truth**
   - Machine Agent heartbeat leases for attached managed sessions
   - bridge/TUI detach or terminal signals
   - owns hosted liveness while the session is managed

The Machine Agent owns idle attached liveness because it runs on the machine
that owns the Codex bridge and can observe local control state. The Runtime Host
owns lease expiry using server receive time so laptop clock skew cannot extend
or shorten hosted liveness.

## Invariants

- `attached + idle` is a valid steady state.
- Managed Codex transcript ingest must not set or refresh `sessions.ended_at`.
- `sessions.ended_at` is archive/display timing, not managed-control liveness.
- A managed Codex session is completed only from explicit terminal/control-path
  truth, not from transcript inactivity.
- Runtime lease freshness is finite and server-expiring. The default lease TTL
  is three heartbeat intervals, and one missed heartbeat must not make an
  attached session unavailable.
- A newer attached lease may recover a session from a previous detach/degraded
  runtime state.
- A fresher attached idle lease supersedes older running/thinking/tool phase
  signals for the same session.
- Engine-local `observed_at` is for ordering only. Server receive time owns
  hosted expiry math. Lease-derived runtime observations are ingested with
  `occurred_at = server_received_at`.
- Process checks for managed Codex are producer-side inputs or diagnostics.
  The hosted timeline consumes persisted runtime state, not ad hoc SSH polling.

## Managed Predicate

The authoritative reducer boundary is the session row:

- `execution_home = managed_local`, or
- `managed_transport = codex_app_server`, or
- managed capabilities already attached to the session row

If a Codex session transitions from imported/unmanaged to managed, transcript
ingest must clear parser-derived `ended_at` and stop writing future
transcript-derived `ended_at` values. Explicit terminal control-path truth is
the only managed-session writer allowed to end the control lifecycle.

There is no implicit managed-to-unmanaged demotion at launch. Once a Codex
session has a managed control path, transcript ingest does not resume ownership
of `ended_at` unless a future explicit demotion event removes managed
capabilities from the session row. That future demotion event must also define
the new archive lifecycle writer before it ships.

## Data Contract

Machine Agent heartbeat payloads may include a `managed_sessions` array.

Each row represents current local control-path truth for one managed session:

- `session_id`
- `provider`
- `machine_id`
- `sequence`
- `state`: `attached`, `detached`, or `degraded`
- `phase`: current managed phase; normalized to `idle` for attached leases when
  absent
- `tool_name`
- `bridge_status`
- `thread_subscription_status`
- `observed_at`
- `lease_ttl_ms`

The Runtime Host converts fresh `attached` rows into runtime phase events:

- `source = engine_attached_lease`
- `kind = phase_signal`
- `phase = idle` unless a fresher specific phase is present
- `freshness_expires_at = server_received_at + lease_ttl`
- `dedupe_key = engine-attached-lease:{machine_id}:{session_id}:{sequence}`

Managed Codex bridge phase events (`source = codex_bridge`) use the same
15-minute freshness budget as attached leases. They are local owner/control
signals, not generic short-lived transcript hints, and must not shorten an
attached lease window when the bridge emits thinking/running transitions after a
heartbeat.

The Runtime Host converts explicit `detached` rows into recoverable
control-path-loss events:

- `source = engine_attached_lease`
- `kind = phase_signal`
- `phase = blocked`
- `tool_name = control path`

Detached is not completed. It does not write `sessions.ended_at`, and it does
not set runtime `terminal_state`.

The only managed Codex control event that writes `sessions.ended_at` is an
explicit final session termination from the managed owner, such as
`terminal_state = session_ended`. That event means the local owner intentionally
ended the provider session rather than merely losing or detaching the bridge.

Lease ordering uses Runtime Host receive time. `sequence` is used for heartbeat
dedupe and audit, not cross-machine ordering, because machine-local sequences
are not comparable. If the same managed session appears from two machines, the
newest attached lease wins. Once the reducer sees explicit
`terminal_state = session_ended`, later heartbeat leases cannot resurrect the
session. Later product work can expose multi-attach as a richer state, but
launch should not let stale machine state hide the currently attached owner.

For TUI-attached managed Codex, `attached` requires the bridge/control socket to
be healthy and the Codex TUI attachment to be present. For detached-UI managed
Codex, `attached` requires a healthy bridge/app-server and an existing thread;
there is no visible TUI by design. If the expected control signal for the
launch mode is missing while the engine still knows about the session, the
lease state is `degraded`, not `attached`.

Fresh `degraded` means the session is still managed and recoverable, but live
control is impaired. Hosted surfaces should show it as attention/degraded, not
completed and not unavailable.

Allowed attached lease phases are `idle`, `thinking`, `running`, `blocked`, and
`needs_user`. Missing phase on an attached lease is normalized to `idle`.
Missing phase on `detached` or `degraded` is allowed.

## Cadence

The engine emits managed-session leases:

- immediately when Codex attaches
- immediately when Codex detaches or degrades
- immediately after engine startup or wake if the session is still attached
- on every normal heartbeat while the session remains attached

The hosted lease TTL defaults to 15 minutes for the current 5 minute heartbeat
cadence. If the cadence changes, the TTL must remain at least three intervals.
Laptop sleep can still expire a lease; after wake the first heartbeat must
restore the session to live idle if local control is still attached.

## Success Criteria

1. A managed Codex session with old transcript activity plus a fresh attached
   heartbeat lease is returned as live idle, not completed or unavailable.
2. Codex transcript ingest for a managed session no longer writes parser-derived
   `ended_at`.
3. Engine heartbeat payloads can report attached managed Codex sessions without
   relying on new transcript writes.
4. Runtime lease expiry is computed from server receive time.
5. Explicit detach or terminal control-path truth overrides idle liveness until
   a newer attached lease arrives.
6. Web timeline/session-card display treats fresh managed idle leases as a real
   ready state.
7. Tests cover server reducer, ingest store, heartbeat ingestion, engine
   heartbeat payload shape, frontend display mapping, and browser-level
   timeline behavior.

Minimum scenario matrix:

- attached plus long transcript idle stays ready/live
- attached with stale running phase becomes idle when a fresher idle lease
  arrives
- attached then explicit detach stops live control until a newer attached lease
  arrives
- sleep/wake-style lease expiry recovers on the next attached lease
- orphan bridge or missing TUI reports degraded, not attached
- explicit final `session_ended` control truth is the only managed Codex path
  that writes `sessions.ended_at`

## Telemetry

Managed liveness telemetry must be reconstructable from SQL and low-cardinality.
Do not label metrics with `session_id`, `workspace`, `tool_name`, bridge path,
or machine-specific free text.

Runtime Host emits:

- `managed_session_heartbeat_lease_rows_total{provider,state,phase}` for each
  managed-session lease row observed in heartbeat payloads before observation
  dedupe
- `managed_codex_runtime_observations_total{source,kind,outcome}` for reducer flow
  through managed Codex sources
- `managed_codex_bridge_freshness_total{outcome}` when Codex bridge phase
  signals receive the managed freshness budget
- `managed_codex_liveness_invariant_sessions{invariant}` as SQL-backed gauges
  for currently violated invariants

The initial invariants are:

- `ended_without_session_ended`: managed Codex session rows with
  `sessions.ended_at` but no runtime terminal state of `session_ended`
- `short_freshness`: managed Codex runtime rows whose current freshness window
  is shorter than the managed liveness budget and whose latest phase signal
  came from a managed Codex source

The gauges are diagnostic overlays, not a second truth store. They refresh from
`sessions`, `session_runtime_state`, and `session_observations` during
Prometheus scrape, so they can be rebuilt after a process restart or queried
directly when debugging a customer report. They must not run in the heartbeat
write lane.

## Rollout

1. Add failing tests for the contract above.
2. Stop managed Codex transcript ingest from ending sessions.
3. Add managed-session leases to Machine Agent heartbeats.
4. Ingest heartbeat leases into `session_runtime_state`.
5. Keep the web UI consuming the runtime view, with display tests for idle
   managed lease state.
6. Run targeted unit tests and a focused E2E timeline check.
