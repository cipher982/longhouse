# Archive Backlog Repair

Status: Draft
Last updated: 2026-06-02

## Problem

The Machine Agent can build a large local archive backlog when the Runtime Host
is offline, overloaded, rebooting, or rejecting archive ingest. That backlog is
valuable durable data, but it must never make live Longhouse feel broken.

The June 2 dogfood incident showed the current gap:

- local health stayed green after live launch was repaired, but `spool_queue`
  still held 6,375 pending archive ranges
- those ranges covered about 16.7GB of local transcript bytes across roughly
  6,374 source files and 6,306 sessions
- the backlog was mostly broad archive discovery, not a few ordinary failed
  HTTP retries
- the largest pending ranges were hundreds of MB each
- host backpressure returned typed `503 Archive ingest backlog is throttled`,
  which correctly protected the Runtime Host, but the product surfaced the
  backlog as an urgent repair state instead of an archive repair mode

This spec defines the product and engineering shape for draining that kind of
backlog safely.

## First Principles

Longhouse has two lanes:

- **Live lane**: answers "what is happening now?" This lane owns managed launch,
  live transcript updates, control WebSocket health, presence, and current
  session state. It must stay fast even when old archives are missing.
- **Durable archive lane**: answers "what provably happened?" This lane owns
  replayable transcript ingest, search, recall, and historical completeness. It
  can trail live state, but it must be correct and recoverable.

The archive lane may be huge and slow. The live lane must not pay for that.

The source of truth for unsent transcript data is still the provider log file on
the user's machine. The local spool should remain a pointer queue into those
files, not a second copy of transcript payloads.

## External Design Guidance

The design should follow these established patterns:

- **Transactional/outbox pattern**: write durable work before publishing it, then
  let a relay/worker publish and mark processed. For Longhouse, the provider log
  is the primary durable source and `spool_queue` is the local relay index.
- **Backoff with jitter**: retries must spread out over time. Fixed retry times
  across thousands of entries create synchronized retry storms.
- **Explicit overload response**: the host should return a specific retryable
  overload signal, and clients should back off instead of retrying immediately.
- **SQLite single-writer reality**: WAL improves read/write concurrency, but the
  Runtime Host still has one writer. Archive repair must assume write bandwidth
  is scarce and preserve health/launch reads.
- **Dead-letter with inspection**: poison ranges should stop retrying forever,
  but they should remain locally inspectable until the user explicitly resolves
  or expires them.

References:

- https://learn.microsoft.com/en-us/azure/architecture/best-practices/transactional-outbox-cosmos
- https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html
- https://sre.google/sre-book/addressing-cascading-failures/
- https://www.sqlite.org/wal.html

## Current Local Model

The local shipper database has these relevant tables:

- `file_state`: per-source cursor state, including `queued_offset` and
  `acked_offset`
- `spool_queue`: pointer ranges with `provider`, `file_path`, offsets,
  `session_id`, `retry_count`, `next_retry_at`, `last_error`, and `status`

Current behavior:

- spool entries are byte-range pointers, not payload blobs
- retry uses exponential backoff up to one hour
- hard cap is 10,000 spool rows
- pending entries older than seven days become dead-lettered; dead entries older
  than 30 days are deleted
- Runtime Host archive backpressure returns typed `503` and the engine defers
  that path without incrementing `retry_count`
- scheduler reserves live slots and caps retry/scan concurrency separately

These are the right mechanics. The missing product contract is how to classify,
budget, drain, and display archive backlog.

## Target Product Semantics

Health states must distinguish live readiness from archive completeness.

Use these independent axes:

- `live_control`: connected, degraded, offline
- `live_ingest`: current transcript shipping, delayed, failed
- `archive_repair`: idle, pending, draining, paused, blocked, dead_lettered
- `archive_completeness`: approximate pending ranges, bytes, sessions, oldest
  pending age, and newest pending age

Rules:

- Live green plus archive backlog is **not** "Longhouse broken".
- Managed launch must stay enabled while archive repair is pending.
- The menu bar may show an archive backlog badge, but it must not replace the
  headline with a red repair state unless live lane health is affected.
- Archive repair actions must be explicit: pause, resume, drain now, drain
  slowly, dead-letter selected poison ranges, inspect examples.
- The default mode after restart is conservative trickle repair, not full replay.

## Drain Policy

Archive repair should be driven by budgets, not by raw pending count.

Recommended initial defaults:

- one archive replay worker per machine by default
- max archive request concurrency per hosted tenant: one request
- max compressed request body: existing `max_batch_bytes`
- max uncompressed bytes read per archive drain tick: 25MB
- max archive wall-clock per drain tick: 2s
- max archive bytes per hour in default mode: 250MB
- pause archive drain while live control heartbeat is late or live transcript
  shipping has recent failures
- full jitter on `next_retry_at`, including host backpressure deferrals

Priority order:

1. live watcher events
2. current managed-session transcript gaps
3. small recent archive gaps
4. old archive gaps below 10MB
5. large archive gaps
6. huge archive gaps over 100MB, opt-in or very slow trickle

This keeps the product useful before historical archive completeness is perfect.

## Runtime Host Policy

The Runtime Host should keep the existing typed backpressure response, but make
it richer:

- include `Retry-After`
- include a stable machine-readable detail code, e.g. `archive_backpressure`
- include optional advisory headers:
  - `X-Archive-Retry-Min-Seconds`
  - `X-Archive-Retry-Max-Seconds`
  - `X-Archive-Drain-Mode: paused|trickle|normal`
  - `X-Ingest-Queue-Wait-Ms`
  - `X-Ingest-Exec-Ms`

The server should reject archive replay before body decode when the single
writer is busy. It should not make archive clients decompress and parse only to
wait behind health-critical work.

Archive replay writes should stay chunked and interruptible. The current
`ingest-replay` and `ingest-scan` chunk size of 100 is a reasonable starting
point for SQLite-hosted dogfood.

## Machine Agent Policy

The Machine Agent needs three explicit modes:

- `live`: current default; watches and ships live events, collects local status,
  drains small hook/runtime outboxes
- `archive-trickle`: default background repair; budgeted by bytes/time and
  paused on live-lane degradation
- `archive-drain`: explicit operator/user action; drains faster but still honors
  host backpressure and live reservation

Implementation requirements:

- add jitter to all deferred `next_retry_at` writes
- track bytes, not only row counts, in spool health
- schedule retry paths by small/recent first, not only oldest first
- cap per-tick bytes and wall-clock, independent of worker count
- use host backpressure to lower local archive budget, not just to delay one
  path
- treat huge ranges as a separate class that can be paused or trickled
- expose the archive limiter snapshot in `engine-status.json`
- do not let startup reconciliation enqueue thousands of scan jobs before the
  first health/control heartbeat is stable

## Operator Surface

Add a CLI and machine API surface for archive repair.

CLI:

```text
longhouse archive status
longhouse archive inspect --limit 20
longhouse archive pause
longhouse archive resume --mode trickle
longhouse archive drain --budget 250MB --max-minutes 30
longhouse archive dead-letter --path <path> --reason <reason>
```

Machine API:

```text
GET  /api/agents/machines/{device_id}/archive-backlog
POST /api/agents/machines/{device_id}/archive-backlog/control
```

The API should expose raw evidence:

- pending/dead counts
- pending bytes
- provider breakdown
- size buckets
- oldest/newest pending times
- top source paths by bytes/count
- recent error classes
- current archive mode and budget
- last successful archive ship
- last host backpressure signal

The user-facing menu bar should consume this summary instead of inferring
severity from `spool_pending` alone.

## Immediate Dogfood Drain Plan

For the current `cinder` backlog:

1. Keep the broad backlog deferred until a controlled drain exists or until an
   operator explicitly starts `archive-drain`.
2. Leave live launch and iOS/session reads green.
3. Run a dry-run profiler over pending entries to estimate event counts and
   compressed request sizes without POSTing.
4. Drain a small recent slice first:
   - only rows below 10MB
   - only one archive request at a time
   - stop on first host backpressure
   - verify menu bar remains live-green
5. Separately inspect huge ranges over 100MB. These are likely transcript files
   that should be split, compacted, or trickled at night.
6. Only after small/recent gaps drain cleanly, resume old broad archive repair.

Do not delete pending rows to make health green. The rows are evidence of
archive incompleteness. Health should learn to describe them honestly.

## Phased Implementation

### Phase 1: Observability and Product Truth

- Add archive backlog summary to local health.
- Split live health from archive backlog severity.
- Add size buckets and byte totals to `engine-status.json`.
- Add tests that a large pending archive backlog does not mark managed launch
  unavailable.

### Phase 2: Safe Scheduler Budgets

- Add jittered retry scheduling.
- Add archive byte/time budgets.
- Prioritize recent/small entries before old/huge entries.
- Pause archive repair on live-lane heartbeat delay or live ship failures.
- Add regression tests for startup with thousands of deferred entries.

### Phase 3: Operator Controls

- Add `longhouse archive status/inspect/pause/resume/drain`.
- Add the machine API archive backlog summary/control endpoints.
- Update the macOS menu bar to show archive backlog as a separate state.

### Phase 4: Large Range Strategy

- Add range splitting for huge backlog entries where parser semantics allow it.
- Add a "large archive paused" state when entries exceed budget thresholds.
- Add dead-letter inspection for stale/missing/replaced files with clear
  operator language.

## Success Criteria

- A 10k-entry archive backlog cannot block `longhouse codex` or
  `longhouse claude` launch.
- iOS/browser session reads stay responsive while archive repair is pending.
- Host SQLite write queue stays below the configured target under archive drain.
- Backpressure causes jittered, budget-wide slowdown rather than synchronized
  one-minute retries.
- Product health can say: "Live control healthy; archive repair pending
  6,375 ranges / 16.7GB" without marking Longhouse broken.
- Operators can drain, pause, inspect, and dead-letter archive backlog without
  ad hoc SQL.
