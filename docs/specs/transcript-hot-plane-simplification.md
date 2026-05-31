# Transcript Hot-Plane Simplification

**Status:** in flight
**Owner:** maintainer
**Date:** 2026-05-19
**Parent spec:** the transcript hot-plane design (internal spec)

## Problem

The hot-plane epic shipped its restructure (one fsevent path + reconciliation
scan) but the live SLA still misses by 60×. Flight-recorder analysis of today's
production traffic (n=125, after the 22:50 deploy):

| stage                       | p50 ms | p95 ms |
| --------------------------- | -----: | -----: |
| observation_window          |  10–80 |  ~250  |
| observation_to_enqueue      |  ~5    |  ~20   |
| **enqueue_to_job**          | 3536   | 14070  |
| job_to_http                 |  ~80   |  ~300  |
| http_latency                |  ~120  |  ~400  |

Spec target is file-append → HTTP-send p95 < 500ms. The whole budget is being
consumed inside `enqueue_to_job` — the gap between "scheduler enqueued ready
work" and "spawn_blocking actually started running it."

Root cause: `DAEMON_MAX_IN_FLIGHT_CAP = 2`. With 5+ active managed sessions this
serialises live work two-at-a-time and the per-priority `LIVE_IN_FLIGHT_CAP =
8` in scheduler.rs is dead code overshadowed by the outer cap. The constant has
been stale since `21b3a13e` (March 2026) and we never noticed.

The deeper problem is configuration drift. The engine has ~14 tunable
constants for tick rates, retry delays, and concurrency caps. Most are
load-bearing in name only. The Jeff Dean cut: delete the layers, don't tune
them. Two knobs in the engine — burst coalescing window and live concurrency —
should be enough to express the policy. Everything else is consequence.

## Goals

1. **Fix the SLA miss.** file-append → HTTP-send p95 ≤ 500ms under
   5-concurrent-sessions stress.
2. **Reduce engine tunable surface area.** Target: ≤ 6 named time/concurrency
   constants in `daemon.rs`, down from 9.
3. **Delete dead code paths** (polling loops, blocking sleeps, redundant caps)
   rather than tune them.
4. **Make stale constants noisier.** Add a stress harness so the next time a
   cap is overshadowed we catch it in CI, not via flight-recorder forensics.

## Non-goals

- Changing the architecture (still: fsevent + reconciliation scan).
- Adding new env knobs — the whole point is fewer.
- Touching the server-side ingest path. The bottleneck is engine-local.

## Plan

Three tiers, three (or four) commits, validated against a synthetic stress
test between each.

### Tier 1 — fix the SLA miss

#### T1.1 — Delete `DAEMON_MAX_IN_FLIGHT_CAP`

- Remove the `min(workers, DAEMON_MAX_IN_FLIGHT_CAP)` outer cap.
- `PathScheduler::new(max_in_flight)` is constructed from `config.workers` (or
  a sensible default like `num_cpus`). The scheduler's per-priority caps
  (`LIVE_IN_FLIGHT_CAP = 8`, `RETRY = 1`, `SCAN = 1`) are now the only truth.
- Net: one constant, one helper function gone.

**Success criteria**

- Before: stress harness shows `enqueue_to_job` p95 > 5s.
- After: `enqueue_to_job` p95 < 500ms with 5 sessions writing concurrently.
- Existing scheduler tests still pass.

#### T1.2 — Replace the watcher polling loop with `await rx.recv()`

- Delete `WATCHER_POLL_INTERVAL = 10ms` and the `watcher_poll_timer.tick()`
  arm in the daemon `select!`.
- Replace `SessionWatcher::try_next_event()` with `next_event(&mut self) ->
  Option<WatcherEvent>` (async — internally `self.rx.recv().await`).
- The select arm becomes `Some(first_event) = watcher.next_event()`.
- The 500ms throttle inside `collect_batch_after` already handles burst
  coalescing; that stays.

**Success criteria**

- File-append → enqueue p95 latency drops by up to 10ms (less if mostly
  bursting).
- No new "watcher channel full" warnings (`WATCHER_CHANNEL_CAPACITY = 2048`
  unchanged).
- `make test-engine` still passes.

#### T1.3 — DROPPED

Today's flight recorder shows `prepare_binding_wait_ms` p95 = 0ms on the
fsevent path. The 300ms sleep only fires on managed-Codex cold-start (a
rare event), and replacing it with a non-blocking re-enqueue carries real
risk of permanently splitting transcripts. The cost isn't worth the cure.

### Tier 2 — delete the dead knobs

#### T2.1 — Hardcode flush_ms; delete the CLI flag

- `flush_ms` lives on `ConnectConfig.flush_interval` (set in
  `main.rs:704-725`); the CLI flag at `main.rs:152` defaults to 500ms.
- Replace with a hardcoded `pub const FLUSH_INTERVAL: Duration =
  Duration::from_millis(100);` near the watcher constants.
- Why 100ms not 50ms: production `observation_window` p95 is ~250ms (some
  bursts span longer than 50ms). 100ms catches most bursts while still
  leaving headroom inside a 500ms end-to-end budget. We can drop further
  later if the stress harness shows it's safe.
- Delete the `--flush-ms` CLI arg, the `flush_ms` field threading through
  `ConnectConfig`, and the flush_interval struct field. Inline the constant
  in `collect_batch_after`.

#### T2.2 — DROPPED from this round

`local_retry_delay()` is used on DB-open failure, spool replay failure,
prepare failure, and ship failure — not just live-vs-non-live retry timing.
Collapsing 5s → 500ms here would create a 10× hotter retry loop on real DB
or network outages. The asymmetry is doing real work: live retries snappy,
fault retries slow.

Defer. If we still want to simplify later, the right cut is to keep the
priority-based delay but rename the constants so the role is obvious
(`LIVE_RETRY_DELAY` vs `FAULT_RETRY_DELAY`).

#### T2.3 — Replace per-priority caps with a Live reservation

- Drop `RETRY_IN_FLIGHT_CAP=1` and `SCAN_IN_FLIGHT_CAP=1`. Replace with one
  invariant: **at least 8 of the global `max_in_flight` slots are reserved
  for Live work.** Concretely: Retry/Scan can occupy at most
  `max_in_flight - LIVE_RESERVED` slots, where `LIVE_RESERVED = 8`.
- This preserves the property that a backlog burst (e.g. reconciliation scan
  after a long sleep) cannot drain all worker slots and stall live shipping.
- Also delete `pop_launchable_live_or_retry()` — currently dead code (always
  called with `allow_retry=false` in production).
- `pop_launchable_live` stays — it's used during offline windows and when we
  want to pre-empt retry/scan during a live burst.

### Tier 3 — make staleness loud

#### T3.1 — Flight-recorder analyzer (now); synthetic harness (later)

**Phase 1 (this round):** ship a small analyzer that parses today's flight
recorder JSONL and prints per-stage p50/p95/p99 plus group-by source. This
turns the existing telemetry into a measurement loop with zero new
infrastructure. We already have N=125+ samples per day on real traffic.

**Phase 2 (deferred):** synthetic 5-session stress harness with real provider
roots, local HTTP echo, and the same analyzer pointed at the harness's own
flight-recorder output. Defer until phase 1 has shown the real bottleneck
moving as we ship Tier 1.

- New subcommand: `longhouse-engine stress --sessions 5 --duration 30s
  --rate 5/s` (events-per-second per session, default 5).
- Use **real provider roots**: the harness creates a temp `~/.claude/projects`
  layout with valid project subdirs and `.jsonl` filenames matching what the
  real Claude CLI writes (`<session-uuid>.jsonl`). This exercises the real
  `discovery::provider_for_path` matcher, FSEvents recursive watch, and
  canonicalization paths, not a mock provider.
- Server-side: spin up a no-op HTTP echo on a local port that ack 200 with
  bounded latency (configurable). This is the `enqueue_to_job → http_latency`
  surface; we want the server out of the picture so we can attribute time
  inside the engine.
- Tails the flight-recorder JSONL emitted during the run, computes percentiles
  for **every stage**, prints a table:
  - `observation_window_ms`
  - `observation_to_enqueue_ms`
  - `enqueue_to_job_ms`
  - `prepare_blocking_queue_wait_ms`
  - `open_db_ms`
  - `prepare_total_ms`
  - `job_to_http_ms`
  - `http_latency_ms`
- Pass criteria (none of these alone are enough; all must hold):
  - `enqueue_to_job_ms` p95 < 500ms
  - `prepare_blocking_queue_wait_ms` p95 < 200ms (catches blocking-pool
    saturation)
  - `open_db_ms` p95 < 200ms (catches SQLite WAL contention)
  - End-to-end `observation_to_http_ack_ms` p95 < 1000ms
- Keep it small — a single Rust binary, no Python harness. Not part of
  `make test-ci`; called from `make stress` and run on demand.

The intent is a "did we just regress live concurrency?" check that runs in
under a minute and produces the same metrics format as the production flight
recorder.

## Out of scope (audit-but-don't-touch this round)

- `LOCAL_STATUS_INTERVAL_SECS=1` vs `FLIGHT_SAMPLE_INTERVAL_SECS=5` — likely
  fine to merge, but they emit to different files and consumers; defer.
- `OUTBOX_DRAIN_INTERVAL=100ms` vs `LOCAL_WORK_TICK_INTERVAL=250ms` — defer.
- `STARTUP_RECONCILIATION_SCAN_DELAY=120s` — feels stale but reducing it
  changes startup load shape; defer.

## Rollout

- Tier 1, 2, 3 each ship as their own commit/push to `main`.
- Run the stress harness locally between each tier and capture before/after
  numbers in the commit message.
- Final flight-recorder check on real production traffic 24h after the last
  push: confirm p95 file-append → HTTP-send < 500ms.

## Codex review (2026-05-19)

Routed through an internal review pass. Key findings
incorporated above:

- **T1.3 was downgraded from "delete the sleep" to "non-blocking re-enqueue
  for managed Codex cold-path."** Risk of permanently splitting a Codex
  transcript by shipping its first batch under the wrong session id was real.
- **T2.2 dropped entirely.** `local_retry_delay` covers fault retries, not
  just live-vs-non-live timing. 5s → 500ms would create a 10× hotter retry
  loop on real DB/network outages.
- **T2.3 reframed.** Per-priority caps replaced with a Live reservation
  rather than naive deletion, so Retry/Scan still can't drain all slots.
  `pop_launchable_live_or_retry` confirmed dead in production and deleted.
- **T2.1 plumbing path corrected** — flush_ms is `ConnectConfig`, not
  `WatcherConfig`. Bumped 50ms → 100ms to cover the observed
  observation_window p95 tail.
- **T3.1 success criteria expanded** to all stages, not just
  enqueue_to_job — otherwise the bottleneck silently moves to SQLite or the
  blocking pool and the harness lies.

## Risk register

| risk                                                | mitigation                                                                |
| --------------------------------------------------- | ------------------------------------------------------------------------- |
| Removing the 300ms binding sleep loses session_ids  | Reconciliation scan re-ships within 30s; observed-only on cold start.     |
| 50ms flush throttles less, more events through API  | API already handles burst (HTTP/2 stream cap=100). Watch ingest p95.      |
| Removing per-priority caps causes Scan to starve Live | Live always pre-empts via early `pop_launchable_live` check; tested.    |
| Stress harness flakes in CI                         | Mark as bench-only; not part of `make test-ci`. Run on demand.            |
