# Hosted Archive Restart Control

**Status:** Implementation spec
**Owner:** Longhouse core
**Created:** 2026-07-08
**Related:** `docs/specs/storage-failure-isolation.md`,
`docs/specs/archive-backlog-repair.md`

## Goal

Hosted Runtime Host and Machine Agent restarts must never resume unbounded
archive replay by default.

The product rule is simple: live launch/control, heartbeat, runtime state, and
current transcript ingest stay fast even when archive replay is hours behind.
Archive replay may pause, trickle, shed, or require operator action. It may not
make the hosted tenant look dead.

## Research Notes

The relevant best practices are mature and boring:

- SQLite WAL can grow without bound when checkpoints cannot complete, especially
  with long readers, disabled/deferred checkpoints, or large write bursts. WAL
  size is therefore a control signal, not just telemetry.
  Source: `https://sqlite.org/wal.html`
- Retry storms and thundering herds happen when clients all resume work or retry
  at once after a degraded period. Services should return typed backpressure,
  include `Retry-After`, and clients should back off instead of retrying in a
  tight loop.
  Source: `https://learn.microsoft.com/en-us/azure/architecture/antipatterns/retry-storm/`
- Queue systems avoid duplicate/infinite processing with claim windows,
  visibility timeouts, heartbeat/extension for active work, bounded retries, and
  dead-letter handling for poison work.
  Sources:
  `https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html`,
  `https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html`

For Longhouse, the launch-practical translation is not a generic queue platform.
It is a small controller loop over existing archive repair modes, shipper
backpressure, local spool state, and Runtime Host health.

## Current State

Already present:

- Engine mode vocabulary: `paused | trickle | drain`.
- Engine archive repair control file with mode and tick-byte behavior.
- Engine status distinguishes archive backlog mode/state from machine liveness.
- Runtime Host archive ingest sheds on writer pressure and now on archive WAL
  pressure.
- Hosted/dogfood service generation already defaults hosted `*.longhouse.ai`
  Runtime Hosts to archive repair mode `paused`; direct CLI remains `drain`.
- Shipper reads typed Runtime Host backpressure and persists archive retry floors
  into the local spool.

Known gaps:

- `longhouse-engine connect` still defaults `--archive-repair-mode` to `drain`.
  That is acceptable for direct self-host/dev usage, but not for hosted dogfood
  or hosted launch install paths.
- Rate-limit and archive-backpressure retry math must treat `Retry-After` as a
  floor and add jitter above it, not shrink it.
- Non-paused restart replay must begin after a small jittered warmup rather than
  immediately queueing archive work.

## Product Contract

### Modes

`paused`

- No startup reconciliation scan.
- No periodic fallback scan.
- No periodic failed-shipment retry replay.
- Existing backlog remains durable and visible.
- Live watcher shipping, runtime events, heartbeat, managed control, and current
  live transcript ingest continue.

`trickle`

- Archive replay is allowed only under conservative budgets.
- It honors Runtime Host `Retry-After`.
- It uses jittered scheduling so restarts do not synchronize replay storms.
- It skips huge ranges unless the Runtime Host reports archive pressure below
  target.

`drain`

- Operator-requested catch-up mode.
- Still bounded by server admission and WAL pressure.
- Never bypasses `Retry-After`.
- Intended for local/self-host/manual repair, not hosted default.

### Defaults

Hosted/dogfood Machine Agent install paths default to `paused`.

Hosted operator-controlled repair starts with `trickle`, not `drain`, unless an
operator explicitly asks for `drain`.

Self-host/dev/manual `longhouse-engine connect` may keep `drain` as the CLI
default, but status must always report the effective mode so the UI can explain
why archive history is paused or catching up.

## Runtime Host Contract

Archive ingest/replay backpressure remains HTTP-level and typed:

- status: `503`
- `Retry-After: <seconds>`
- `X-Ingest-Lane: archive`
- `X-Ingest-Backpressure: archive_ingest_backpressure`
- `X-Ingest-Admission-State: archive_wal_pressure | archive_writer_busy |
  writer_queue_pressure | archive_slots_full | request_budget_exhausted |
  writer_timeout | writer_queue_timeout | writer_interrupted`

Runtime Host health surfaces:

- archive WAL bytes and shed threshold;
- last checkpoint `busy`, `log_frames`, `checkpointed_frames`, and
  `remaining_frames`;
- archive degraded reason;
- write serializer active label/age/queue depth;
- live writer status separately from archive writer status.

`/readyz` behavior:

- archive pressure: `200` with `ready_with_archive_degraded`;
- live writer pressure: `503`;
- DB unavailable / missing required FTS table: `503`.

## Machine Agent Contract

The engine should treat archive replay as a rate-limited repair loop.

When Runtime Host returns archive backpressure:

- persist the failed path/range for later replay;
- store the server-provided retry floor;
- add bounded jitter before the next attempt;
- do not enqueue the same path/range repeatedly while it is deferred;
- do not let deferred archive work consume live scheduler reservations.

Trickle mode pacing:

- base tick: existing `ARCHIVE_TRICKLE_TICK_BYTES`;
- per-restart warmup: first archive attempt delayed by a small jittered window;
- after each archive backpressure response: next attempt is no earlier than
  `Retry-After + jitter`;
- if Runtime Host remains archive-degraded, keep trickle at minimum budget;
- if Runtime Host is healthy and archive WAL is below shed threshold, allow one
  bounded archive batch per tick.

Drain mode pacing:

- larger tick-byte budget than trickle;
- still observes all server backpressure and retry floors;
- still skips huge replay ranges when Runtime Host pressure says no;
- logs mode transitions and first replay attempt after restart.

## Operator Control

Use one mode enum everywhere: `paused | trickle | drain`.

Operator controls should write the existing archive repair control file. A
server-command mode source is deferred until there is a real command channel that
needs it.

Minimum CLI/API operations:

- show effective mode and source: startup default or control file;
- set mode to `paused`;
- set mode to `trickle`;
- set mode to `drain`;
- show backlog count/oldest age/next retry time;

Do not build a generic job ledger before launch. If a repair item is poisonous,
the immediate prelaunch behavior is: preserve the path/range, record last error
and attempt count, stop tight replay, and make it visible.

## Observability

Runtime Host logs:

- archive admission decision and reason;
- WAL shed threshold/value;
- writer active label/age when shedding;
- mode headers if returned to engine.

Machine Agent logs/status:

- effective archive mode and source;
- first archive replay attempt after process start;
- backpressure response kind and `Retry-After`;
- computed next retry time;
- backlog count and oldest backlog age;
- skipped replay because mode is `paused`;
- skipped replay because live scheduler is busy.

Metrics already present should remain the first source of truth. Add only these
if missing from status/health:

- `archive_backlog.mode`
- `archive_backlog.state`
- `archive_backlog.pending_ranges`
- `archive_backlog.oldest_pending_age_ms`
- `archive_backlog.next_retry_at`
- `archive_backlog.last_backpressure_kind`

## Acceptance Gates

### Restart Safety

Given a hosted-mode Machine Agent restart with local archive backlog:

- no `ingest-scan` request is sent during the startup warmup when mode is
  `paused`;
- no `ingest-replay` request is sent while mode is `paused`;
- heartbeat, runtime events, and live transcript shipping still run;
- local health reports online plus `archive_backlog.mode="paused"`;
- Runtime Host `/readyz` remains `ok` or `ready_with_archive_degraded`, not 503.

### Trickle Safety

Given mode changes from `paused` to `trickle`:

- at most one bounded archive batch is attempted per tick;
- first attempt has jitter so a fleet restart does not synchronize;
- `Retry-After` is honored on typed archive backpressure;
- archive WAL pressure prevents further replay until WAL drains;
- live transcript ingest succeeds while trickle replay is being rejected.

### Drain Safety

Given operator sets mode to `drain`:

- archive replay rate increases compared with trickle;
- server admission can still shed it;
- huge ranges are skipped when Runtime Host pressure is above target;
- switching back to `paused` stops new archive replay within one scheduler tick.

### Regression Scenario

Recreate the July 8 class:

- archive backlog exists locally;
- Runtime Host archive WAL is above shed threshold or archive writer is wedged;
- Machine Agent restarts;
- archive replay is paused or receives typed backpressure with `Retry-After`;
- live transcript ingest and heartbeat continue;
- `/readyz` reports ready with archive degraded, not hard down;
- no background archive write pins the live lane.

## Implementation Order

1. **Hosted default resolver**
   - Already present in service generation and native service repair.
   - Hosted/dogfood install path uses `paused`.
   - Direct CLI default can remain `drain`.
   - Existing tests prove the mode by launch path.

2. **Engine restart warmup**
   - In `trickle`/`drain`, delay first archive replay after restart by a small
     jittered window.
   - Do not delay live watcher, runtime outbox, heartbeat, or control.

3. **Backpressure retry floor**
   - Server `Retry-After` is already persisted per replay path/range.
   - Fix retry math so jitter is added on top of the floor.
   - Avoid duplicate scheduling while deferred.

4. **Status surface**
   - Ensure local status and heartbeat expose mode, state, pending range count,
     oldest age, and next retry time.

5. **Acceptance test**
   - Add a hosted restart regression that proves paused/trickle behavior under
     archive WAL pressure while live ingest succeeds.
   - Prove paused mode holds across periodic retry/reconciliation ticks.
   - Prove control-file `paused -> trickle` starts archive repair without a
     process restart.

## Non-Goals

- No Redis, Kafka, Temporal, SQS, or generic job platform.
- No Postgres requirement.
- No broad hosted self-serve repair UI before launch.
- No automatic full-history repair on restart.
- No hidden fallback from paused archive repair into drain.

## Decision

For hosted launch, default archive replay is `paused`. Operator repair starts in
`trickle`. `drain` is explicit. Runtime Host remains authoritative for archive
admission, and Machine Agent must honor typed backpressure rather than trying to
outsmart it locally.
