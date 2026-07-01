# Write Lane Isolation

## Problem

Longhouse currently protects SQLite from concurrent writers with one global
`WriteSerializer`. That fixed lock storms, but it still lets unrelated write
classes share the same latency fate. A historical archive backlog, notification
bookkeeping, heartbeat snapshots, live runtime observations, and managed-launch
session creation all ultimately wait for the same writer slot.

That is the wrong product contract. A six-month-old transcript repair is
durability work; a managed TUI session becoming visible/control-ready is live
work. The former may trail and retry. The latter must fail fast or complete
quickly enough that launch, local-health, and remote control remain trustworthy.

The current incident class is:

- archive or maintenance writes occupy the single writer long enough that hot
  routes queue behind them
- machine-facing clients wait until HTTP timeout, then retry in ways that add
  more pressure
- local-health shows broad degradation even though the system is not CPU-bound
  and the user workload is small

The fix is not a softer menu-bar color. The fix is to make the Runtime Host
explicitly understand write temperature and admission.

## Goals

- Keep managed launch, control results, live runtime observations, presence,
  and heartbeat liveness out of unbounded request waits.
- Preserve archive correctness and replayability without letting archive repair
  monopolize the writer.
- Give the Machine Agent typed server backpressure it can respect instead of
  generic timeouts.
- Centralize write-lane policy so future SQL fixes do not reimplement
  thresholds in one router at a time.
- Keep the first implementation SQLite-only and compatible with self-hosted
  installs.

## Non-Goals

- Do not migrate the production database file or split hot/cold storage in this
  first slice.
- Do not replace SQLite or introduce Postgres.
- Do not hide health warnings without reducing the underlying failure mode.
- Do not drop raw transcript/archive evidence.

## Data Temperature Model

### Hot

Hot writes are part of the active control loop. They should either commit fast
or return typed backpressure before the client hits its own timeout.

Examples:

- `managed-launch`
- `machine-control-result`
- `remote-launch-result`
- `runtime-live`
- `runtime-observations`
- `ingest-live`
- `presence`
- `heartbeat` when it carries managed leases or runtime snapshots

### Warm

Warm writes are user-visible but not terminal-critical. They should stay behind
hot work and ahead of archive repair.

Examples:

- `runtime-push`
- session attention notification stamps
- timeline projections for recent sessions
- summary/title/task bookkeeping

### Cold

Cold writes are reconstructable or historically important but not latency
critical. They must be chunked, bounded, and explicitly deferred under pressure.

Examples:

- `ingest`
- `ingest-replay`
- `ingest-scan`
- archive shadow manifests
- projection reconcile/backfill
- embedding/backfill jobs
- old heartbeat history retention cleanup

## Current State

The server already has useful pieces:

- `WriteSerializer` exposes queue depth, active label, active age, queued labels,
  per-label timing, and `repair_idle_queue()`.
- Ingest has local admission helpers for archive and live ingest.
- The engine already parses typed `503` backpressure for
  `archive_ingest_backpressure` and `live_ingest_backpressure`.
- Managed launch now refuses to sit behind a stale active writer.

The missing piece is shared policy across all machine-facing hot routes. Runtime,
presence, heartbeat, and machine presence can still wait behind the single
writer until their request timeout.

## Design Correction

`WriteSerializer` priority already decides who runs next, so pre-queue
admission does not fix an active cold writer that already owns the slot. A hot
write behind a cold write still waits until the active writer yields.

`timeout_seconds` on `WriteSerializer.execute()` only wraps the worker after the
request has acquired the writer slot. It does not bound queue wait. Hot routes
therefore need a queue-wait timeout in the serializer itself, not only
route-level admission and not only the existing execution timeout.

The revised first slice is:

- add queue-wait timeout support to `WriteSerializer`
- apply it to hot machine-facing routes
- return typed hot-write backpressure on queue timeout
- identify heartbeat splitting as the next highest-leverage server change
- defer the larger shared `write_lanes` policy module until more callsites need
  it

## First Implementation Slice

### 1. Bound Hot Queue Wait in `WriteSerializer`

Extend `WriteSerializer` with `queue_timeout_seconds`.

- It starts when a write is queued.
- If the queued request does not receive the writer slot in time, remove it from
  the queue and raise `TimeoutError`.
- Existing `timeout_seconds` keeps its current meaning: execution/slot-hold
  timeout after promotion.
- Record no write timing for queue-timeout calls because no write ran.

### 2. Add Typed Hot Backpressure Responses

Add a small helper for non-ingest hot write pressure headers:

- `Retry-After`
- `X-Longhouse-Write-Backpressure: hot_write_backpressure`
- `X-Longhouse-Write-Lane: hot`
- `X-Longhouse-Write-Admission-State`
- `X-Longhouse-Writer-Queue-Depth`
- `X-Longhouse-Writer-Active-Label`
- `X-Longhouse-Writer-Active-Age-Ms`

Runtime, presence, heartbeat, and machine presence catch serializer queue
timeouts and return `503` with these headers. This converts a silent request
timeout into an explicit Runtime Host pressure signal.

### 3. Apply Hot Queue Timeout to Machine-Facing Routes

Initial thresholds:

- runtime batch: queue timeout 2s
- presence: queue timeout 2s
- machine presence: queue timeout 2s
- heartbeat stamp: queue timeout 2s
- heartbeat bookkeeping: warm priority, execution timeout, no request-blocking
  requirement

### 4. Split Heartbeat Stamp from Bookkeeping

Heartbeat is currently a fat write: it inserts the heartbeat row, trims history,
upserts managed leases, marks missing leases, generates runtime events, and can
reattach sessions. That means heartbeat can itself become the writer that blocks
the live loop.

The second slice should split it into:

- `heartbeat-stamp`: insert the new `AgentHeartbeat` row and minimal liveness
  fields with hot priority and queue timeout
- `heartbeat-bookkeeping`: managed lease reconciliation, missing-session
  marking, runtime events, and retention cleanup as warm follow-up work

The HTTP route can return after the stamp succeeds. Bookkeeping completion
should publish session updates when it finishes, but it should not hold the
heartbeat request open.

Success criteria:

- A heartbeat response can complete after the `heartbeat-stamp` write even when
  `heartbeat-bookkeeping` is still queued.
- The request-scoped DB session is still closed before either serialized write
  waits on the writer.
- Managed lease, unmanaged binding, runtime-event, and stale-row cleanup
  behavior remains intact after bookkeeping drains.
- `heartbeat-bookkeeping` has lower priority than live runtime/control writes.

### 5. Teach Engine Small-JSON Posts to Recognize Hot Backpressure

`ShipperClient::post_json_with_timeout` currently turns all non-2xx statuses
into generic errors. Update it to parse `X-Longhouse-Write-Backpressure` and
`Retry-After` for small JSON routes.

For the first slice, callers do not need perfect adaptive behavior. It is enough
that logs and status can distinguish host admission from network failure. Then
the daemon can cool down heartbeat/presence/runtimes instead of immediately
retrying into pressure.

## Follow-On Architecture

The first slice is admission and protocol. If that stabilizes the live loop, the
next larger win is physical/write-shape separation:

- hot SQLite file on local low-latency storage for sessions, runtime state,
  control results, and current timeline projections
- cold archive sidecar SQLite/object chunks for raw source lines, historical
  transcript ranges, embeddings, and backfill artifacts
- asynchronous projection from cold archive into hot searchable summaries
- explicit session temperature heuristics:
  - active managed session: hot
  - session active in the last day: warm
  - old ended session with complete transcript: cold
  - old session undergoing repair/backfill: cold unless user opens it

That change should be designed separately because it touches storage layout,
backup/restore, and migration.

## Success Criteria

- Under simulated writer pressure, archive ingest receives typed archive
  backpressure and hot routes do not wait until HTTP timeout.
- Under simulated stale active writer, runtime/presence/heartbeat/machine
  presence receive typed hot-write backpressure with retry headers.
- Under healthy writer state, runtime/presence/heartbeat behavior is unchanged.
- Managed launch remains protected and returns a clear retryable error instead
  of hanging behind the writer.
- Local-health can distinguish host write backpressure from generic connect
  errors/timeouts.

## Test Plan

- Backend unit tests for `write_lanes` decisions:
  - healthy writer admits hot/archive
  - archive denied by archive-active writer
  - hot denied by queue hard limit
  - hot denied by stale active writer
  - idle queue repair is invoked before denial
- Backend route tests for:
  - runtime batch `503` hot backpressure headers
  - presence `503` hot backpressure headers
  - heartbeat `503` hot backpressure headers
  - archive/live ingest keeps existing header contract
- Engine tests for:
  - parsing `X-Longhouse-Write-Backpressure`
  - small JSON route errors classified separately from connect errors
  - heartbeat/presence retry cooldown does not poison archive ship stats
- Post-deploy checks:
  - `/api/readyz` reports writer ready
  - local-health `launch_readiness` remains ready while archive backlog drains
  - no runtime/presence/heartbeat route shows repeated request-timeout errors
    during archive replay

## Review Questions

- Is `hot_write_backpressure` the right new protocol, or should non-ingest hot
  writes reuse `live_ingest_backpressure` for engine simplicity?
- Should heartbeat split into a tiny hot liveness stamp plus colder runtime
  snapshot/retention work?
- Are the hot-lane thresholds too conservative for one user, or too lenient
  because one stale writer already means a broken control loop?
- Which cold writes should be physically split first if admission alone is not
  enough?
