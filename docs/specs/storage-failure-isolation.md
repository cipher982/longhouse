# Storage Failure Isolation

**Status:** Approved implementation plan
**Owner:** Longhouse core
**Created:** 2026-07-10
**Related:** `reliability-data-plane.md`,
`hot-cold-runtime-reliability-hardening.md`,
`sqlite-data-plane-completion.md`

## Decision

Longhouse will keep SQLite as the required core database, but cold SQLite work
must no longer execute inside the Runtime Host API process.

The deployed live/archive file split is a capacity and lock-contention boundary.
It is not a crash boundary. A native SQLite failure in either file can terminate
the one Python process that currently owns both, taking live control, auth,
timeline, and health down together.

The launch architecture is therefore:

```text
Runtime Host API process
  owns: longhouse-live.db, live request handling, durable outbox production
  reads: bounded catalog views still resident in longhouse.db during migration
  never runs: cold WriteSerializer work, archive repair, replay, projection

Archive worker process
  owns: longhouse.db writes, LiveArchiveOutbox drain, repair, replay, projection
  supervised: crash/restart/backoff is independent of Runtime Host availability

archive/
  immutable, checksummed raw transcript truth

retrieval.db and future detail indexes
  rebuildable and process-isolated
```

Process ownership is the first required boundary. A later catalog file split or
hosted PostgreSQL adapter must not delay it.

## Incident Evidence

On 2026-07-10 the hosted dogfood Runtime Host stopped answering requests and
later exited `139`. The kernel recorded a segfault in CPython's `_sqlite3`
extension. The live SQLite file remained small and healthy, but it did not keep
the product available because archive and live work shared one process.

Immediately before the crash, orphan-subagent relinking repeatedly attempted an
event move that violated the event deduplication key. The direct data bug is fixed
by `3de8fc3bd`, and hosted containers now use `unless-stopped`. Those changes
reduce recurrence and outage duration; neither contains a future native cold-store
failure.

The existing saturation test blocks an archive writer thread while keeping the
process and archive read engine alive. It proves queue separation, not crash
isolation.

## Product Invariants

1. Live launch, input, runtime, heartbeat, and health remain available while the
   archive worker is stopped, wedged, restarting, or crash-looping.
2. Raw transcript evidence is acknowledged only after immutable archive bytes are
   durably sealed, or the Machine Agent retains retry intent.
3. The monolith has exactly one writer process at a time.
4. Outbox delivery is at-least-once and every projector is idempotent.
5. A timeout never pretends a Python worker thread was cancelled. Killable work
   runs in the worker process.
6. Archive degradation is visible but does not turn hot readiness into 503.
7. No destructive reclaim, raw deletion, or live `VACUUM` is part of failure
   isolation.

## Current State

The useful seams already exist:

- `longhouse-live.db` owns launch readiness, runtime state, input receipts,
  machine-control operations, heartbeat stamps, and `LiveArchiveOutbox`.
- `FilesystemArchiveStore` writes content-addressed compressed chunks using a
  temporary file, fsync, atomic rename, and directory sync.
- `drain_live_archive_outbox` commits archive state before marking a live outbox
  row drained.
- recall already uses a short-lived helper process for `retrieval.db` reads.

The dangerous seam is `_drain_live_archive_outbox_once()` in `maintenance.py`:
the Runtime Host opens both live and archive sessions, then runs the drain through
the in-process archive `WriteSerializer`.

## Worker Contract

### Ownership

The archive worker is the only process allowed to configure or execute the
archive `WriteSerializer` after cutover. It may open the live store only for
short outbox claim/ack transactions. It must release the live transaction before
starting cold work.

The Runtime Host continues producing live facts and outbox rows atomically. It
does not pass SQLAlchemy sessions, engines, ORM instances, or open descriptors to
the worker.

### Durable IPC

`LiveArchiveOutbox` is the v1 IPC protocol. Rows carry a stable kind,
idempotency key, versioned JSON payload, attempt count, and completion state.

The worker loop is:

1. read the next eligible row identity from the live store;
2. release the live transaction;
3. apply one bounded idempotent archive operation;
4. commit archive state;
5. mark the live row drained in a short live transaction;
6. report progress/health to a worker status file.

If the worker dies between steps 4 and 5, the row remains pending and the
idempotent archive operation is retried.

### Supervision

The Runtime Host starts one worker child and observes its exit status. The
supervisor uses bounded exponential backoff, reports crash count and last exit,
and never exits the API merely because the worker is unhealthy.

Shutdown is explicit: terminate, wait for a short grace period, then kill. A
second Runtime Host process cannot start a second archive worker for the same
data root; the worker owns a filesystem lock.

### Health

`/api/health` reports worker state separately from live readiness:

- `running`: child alive and recent progress/idle heartbeat;
- `degraded`: child stopped, stale, backing off, or last operation failed;
- `disabled`: explicit configuration only;
- `unknown`: status evidence unavailable.

`/api/readyz` remains ready when the live store is healthy and only the archive
worker is degraded. The response reason is `archive_worker_degraded`.

## Migration Rules

- Cut over by operation kind or whole writer ownership; never let API and worker
  race the same kind.
- Do not hold a live write transaction while opening or mutating the archive DB.
- Do not use pickle or multiprocessing queues containing ORM objects.
- Do not add Redis, Kafka, gRPC, or another durable queue for v1.
- Do not reintroduce the deleted `LONGHOUSE_HOT_DATABASE_URL`,
  `LONGHOUSE_DERIVED_DATABASE_URL`, or unwired projector scaffolding.
- Reclaim and cache tuning remain independent size/performance work.

## Implementation Phases

### Phase 0: Spec and baseline

- Commit this decision.
- Record the 2026-07-10 crash as the baseline.
- Review the plan independently before runtime changes.

Gate: the worker ownership and no-dual-writer rules are explicit.

### Phase 1: Crash-containment harness

Add a subprocess integration harness that starts a Runtime Host with separate
live/archive files and a fault-injectable archive worker.

The harness must prove:

- killing the worker does not kill the API;
- `/api/readyz` remains hot-ready with archive degradation;
- heartbeat/runtime hot writes continue;
- the selected outbox row remains pending after a pre-commit worker death;
- supervision restarts the worker and the row drains once;
- repeated worker crashes enter backoff rather than a tight fork loop.

Gate: the harness fails against the in-process drain and passes after Phase 2.

### Phase 2: Worker and heartbeat cutover

- Add the archive-worker entrypoint and supervisor.
- Move `heartbeat_stamp.v1` projection to the worker.
- Disable in-process handling of the worker-owned kind.
- Add worker health and status evidence.

Heartbeat is first because its archive projection is idempotent on
`(device_id, received_at)` and has no live-side mutation after archive commit.

Gate: crash harness green, heartbeat archive parity green, no dual handling.

### Phase 3: Complete outbox ownership

Move the remaining outbox kinds in this order:

1. runtime events;
2. managed-local launch;
3. remote launch and outcome;
4. session input receipts.

For kinds that update live state after archive commit, split the current drain
into prepare/apply/ack operations so retries cannot regress newer live state.

Gate: all outbox kinds drain only in the worker; managed launch, input, runtime,
and heartbeat remain usable throughout a worker crash/restart test.

### Phase 4: Complete cold-writer ownership

Move every remaining archive `WriteSerializer` label out of the API process,
including durable ingest, scan/replay, repair, projection, enrichment, and cold
maintenance. The request surface either writes an immutable spool/archive chunk
or submits a bounded worker job and returns typed retryable pressure.

Gate:

- the API process never configures the archive serializer;
- a repository guard test enumerates and rejects archive write call sites in API
  modules;
- the worker is the sole monolith writer under mixed ingest/control load;
- cold native exit leaves auth, timeline catalog, launch, input, runtime, and
  heartbeat available.

### Phase 5: Catalog and read isolation

The API still reads bounded catalog tables from `longhouse.db` during the writer
cutover. If a missing, corrupt, or native-failing monolith read can still take the
product loop down, migrate these bounded tables into the live/catalog store:

- users, refresh sessions, device tokens;
- sessions and thread identity;
- materialized timeline cards;
- control connections/capabilities;
- archive manifest pointers and projection lag.

Event/source-line/observation/FTS tables remain cold and rebuildable. Route
detail/search through a process boundary or completed projection artifacts.

Gate: the API boots and serves the complete launch loop with the cold DB path
unavailable.

### Phase 6: Reclaim and operational proof

After isolation is live and restore evidence is current:

- run the separately approved Phase E clean-store reclaim;
- tune cache/mmap/FTS only against the post-reclaim file;
- run worker-only restore and outbox replay drills;
- soak mixed live/archive traffic with worker crash injection.

Gate: exact-SHA hosted smoke and dogfood prove that cold failure changes archive
lag, not Runtime Host availability.

## PostgreSQL Decision

Do not introduce a dual ORM backend during this migration. The current code passes
SQLAlchemy sessions through hundreds of functions, so dialect branching would
multiply the persistence surface before failure ownership is fixed.

Hosted PostgreSQL becomes eligible only after process isolation when one of these
is measured:

- sustained hot catalog write contention with bounded transactions;
- multiple Runtime Host API replicas need concurrent tenant writes;
- hosted RPO/RTO requires managed failover and point-in-time recovery;
- fleet-scale per-tenant SQLite migrations/backups become the dominant cost.

Raw transcript bytes remain in immutable filesystem/object chunks either way.

## Acceptance

The epic is complete when:

- a cold SQLite worker can be killed or made to exit natively without killing the
  Runtime Host;
- all cold writes have one process owner;
- hot launch/control/input/runtime/heartbeat and timeline catalog survive cold
  failure;
- archive lag is durable, visible, retryable, and idempotently recoverable;
- backup/restore covers live DB, pending outbox, archive chunks, derived stores,
  and worker cursors;
- exact-SHA deployment and dogfood QA pass with a pending archive backlog;
- obsolete in-process drain and cold-writer paths are deleted.

## Non-Goals

- No full PostgreSQL event archive.
- No distributed queue or cache service.
- No weakening raw transcript fidelity.
- No attempt to make native SQLite work safely cancellable inside one process.
- No claim that automatic container restart is equivalent to fault isolation.
