# Hot/Cold Ingest Isolation

**Status:** Launch implementation spec
**Owner:** Longhouse core
**Created:** 2026-06-29
**Branch:** `epic/hot-cold-ingest-isolation`
**Reviewed:** Hatch Opus initial architecture review, 2026-06-29
**Related:** `docs/specs/reliability-data-plane.md`,
`docs/specs/archive-backlog-repair.md`,
`docs/specs/transcript-hot-plane-simplification.md`

## Executive Summary

Longhouse hosted must remain launchable, steerable, and writable while
historical transcript repair is hours or days behind.

The `david010` incident showed that the current single SQLite file is not the
immediate problem by itself. The launch blocker is that hot product writes and
cold archive repair share one active writer slot, and cold repair is allowed to
start automatically and then keep that slot after request timeout. Priority
chooses the next queued item; it cannot preempt a cold write already running
inside SQLite.

This launch epic therefore ships the smallest decisive fix:

1. Hosted machine agents run with archive repair paused by default. Live
   shipping, runtime events, heartbeat, presence, and managed control remain on.
2. Server archive ingest yields between bounded chunks and does not use the
   timeout path that leaves a background cold write pinning the writer slot.

The larger physical data-plane split remains the right long-term architecture,
but it is not part of this launch incident epic. That work stays in
`docs/specs/reliability-data-plane.md`.

## Product Rule

Hot behavior must be independent of cold archive repair.

Hot behavior:

- managed launch and control
- live transcript deltas
- runtime events
- presence, heartbeat, and machine presence
- local health and menu-bar status
- bounded session list and timeline cards

Cold behavior:

- startup reconciliation scan
- periodic fallback scan
- spool replay
- archive backfill and repair
- source-line/event persistence
- FTS/search/detail projection
- embedding and summary backfills

Cold behavior may lag, pause, shed load, or show an indexing state. It must not
block hot behavior.

## Incident Evidence

Observed production symptoms on `david010`:

- `/api/health` reported `writer_active=true` for minutes.
- Active labels included `ingest-live` and `ingest-scan`.
- Queued labels included `runtime-live`, `runtime-observations`, `presence`,
  `heartbeat`, and `ingest-live`.
- When the writer queue cleared, normal runtime writes were millisecond-scale.
- Red/yellow menu bar state tracked hosted write pressure accurately.
- Stopping local `longhouse-engine connect` and restarting `longhouse-david010`
  restored the hosted writer, but managed local startup degraded while the
  shipper was offline.
- Raising local launchd `--fallback-scan-secs` and `--spool-replay-secs` to one
  day did not fully help because the engine also starts a one-shot startup
  reconciliation scan after `STARTUP_RECONCILIATION_SCAN_DELAY`.
- Commit `293c7144e269ba7f4325512340d9e929df0eb4bf` shrank live/archive batch
  targets to 64 KiB. Keep it as mitigation; do not treat it as the solution.

## Root Diagnosis

The current system has four separate problems that compound:

1. **Shared writer slot.** `WriteSerializer` has one active writer. Its priority
   queue is useful before execution starts, but `write_serializer.py` returns
   early from promotion while `_writer_active` is true.

2. **Timeout without release.** `WriteSerializer.execute(...,
   timeout_seconds=...)` returns `TimeoutError` to the caller while the worker
   continues in the background and keeps the writer slot active until it
   completes. That can be acceptable for short idempotent maintenance writes. It
   is not acceptable for archive ingest on a 100 GB+ tenant.

3. **Existing admission control is necessary but insufficient.**
   `_acquire_archive_ingest_slot()` already limits archive ingest and sheds
   before enqueue when the serializer is hot or stale. The incident happened
   despite that because admission control cannot release an already-running cold
   write. The decisive server fix is bounded cooperative progress: release the
   writer between archive sub-batches, then re-check hot pressure before
   continuing.

4. **Startup and backlog pressure.** The engine starts retry/reconciliation work
   automatically. Existing `archive-repair-control.json` paused mode only gates
   part of replay behavior; it does not fully gate startup reconciliation,
   periodic fallback scanning, and initial retry-path queueing.

SQLite itself is not being replaced for launch. We are removing the behaviors
that let cold archival work monopolize the one SQLite writer.

## Launch Decision

Hosted archive repair defaults to **paused/operator-triggered** for dogfood and
launch containment.

That means hosted users get live session sync, searchable recent/hot state, and
managed control as the product contract. Historical repair and backlog replay
are allowed to run only when the operator explicitly enables them or invokes a
bounded repair command. This is intentionally conservative: a stale archive is
recoverable; a hosted instance that cannot launch or write is a product outage.

For paid hosted launch, this decision also requires an explicit backup/restore
and backlog-drain gate: paused backlog must survive restart/backup/restore and
must be drainable later under operator control. Dogfood can ship the containment
first because it is better than repeatedly wedging the live tenant.

Self-hosted local installs may keep archive repair in the existing `drain`
default, but the mode must be explicit in engine status so the user and server
can tell the difference between "repair paused" and "machine down."

## Non-Goals

- No Postgres requirement for core Runtime Host.
- No LiteFS cluster design for launch.
- No Kafka, Redis, or external queue requirement for core.
- No destructive production migration, raw deletion, compaction, or live
  `VACUUM` in this epic.
- No weakening raw transcript fidelity.
- No hidden fallback where paused cold repair silently becomes data loss.
- No reintroduction of the removed `hot.db` / `derived.db` runtime scaffolding
  in this launch epic.
- No Litestream/WAL backup work in this launch epic. Backup replication is
  important, but it does not fix writer starvation.

## Target Shape For This Epic

### Phase 0: Spec and Review

Deliverables:

- this spec committed
- Hatch Opus review incorporated
- exact success criteria written before implementation

Acceptance:

- no behavior change
- worktree branch is clean
- hosted tenant is not left wedged by planning work

### Phase 1: Engine Archive Repair Mode

Add first-class archive repair mode to `longhouse-engine connect`.

Expected CLI/config surface:

- `--archive-repair-mode paused|trickle|drain`
- default:
  - hosted/dogfood launchd path: `paused`
  - self-host/dev/manual engine path: existing default `drain`, unless
    configured otherwise

When mode is `paused`, disable:

- startup reconciliation timer
- periodic fallback reconciliation
- periodic spool replay
- initial failed-shipment retry-path queueing

When mode is `paused`, continue:

- live file watcher shipping
- runtime events/outbox
- heartbeat and machine presence
- managed observation/control paths

Implementation rule:

- reuse the existing mode vocabulary already used by archive repair control:
  `paused`, `trickle`, and `drain`;
- do not introduce a parallel `enabled` value;
- define precedence explicitly:
  - an explicit CLI flag is the startup default and is written into local status;
  - an operator control file may move the running engine between
    `paused`/`trickle`/`drain`;
  - unknown values normalize conservatively to `paused` for hosted/dogfood and
    to the current self-host default only outside hosted install paths.

Status/health must report archive repair mode separately from machine liveness.
Do not make "paused" look like "offline."

The scan/retry gate points are acceptance-critical:

- startup timer branch around `engine/src/daemon.rs`
  `STARTUP_RECONCILIATION_SCAN_DELAY`;
- periodic fallback scan branch;
- `maybe_start_reconciliation_scan(...)`;
- initial and periodic `queue_failed_shipment_retry_paths(...)`.

Tests:

- engine test: paused mode does not queue initial retry paths.
- engine test: paused mode does not arm or run startup reconciliation.
- engine test: paused mode skips periodic fallback scan and spool replay.
- engine test: paused mode never emits `WorkPriority::Scan`.
- engine test: CLI flag and operator control file precedence is deterministic.
- engine test: live watcher jobs still use `WorkPriority::Live`.
- status test: archive repair mode is visible in engine status/local health.
- status test: `archive_backlog.mode="paused"` coexists with green machine
  liveness/presence.
- operator test: paused can transition to `trickle` or `drain` and bounded
  archive ingest resumes.

Success criteria:

- Running the machine agent in paused mode does not create hosted `ingest-scan`
  or `ingest-replay` writes during deterministic unit tests and during the
  smoke backstop window.
- Managed launch/control prerequisites remain available.

### Phase 2: Server Cooperative Archive Ingest

Keep the existing `_acquire_archive_ingest_slot()` admission gate, but stop
treating it as the whole fix.

Server archive ingest must:

- split cold archive persistence into bounded sub-batches;
- release the `WriteSerializer` writer slot between sub-batches;
- re-check hot pressure through existing health/admission state before each
  next sub-batch;
- return 503 / shed archive work when hot pressure is present;
- avoid `asyncio.shield` timeout semantics for cold archive labels, because a
  timed-out archive request must not leave a background cold write pinning the
  hot writer slot.

Important limit:

- We cannot interrupt an individual SQLite statement after it starts. The unit
  of preemption is the archive sub-batch. Therefore sub-batches must stay small
  enough that a single sub-batch cannot violate the hot write SLO by itself.
- "Bounded" must be a concrete byte/row cap, not just an intention. The cap must
  be tested against the serializer's execution/wait metrics.

Tests:

- backend regression: `_acquire_archive_ingest_slot()` still sheds archive work
  when the serializer is hot/stale.
- backend regression: a timed-out cold archive operation does not continue in
  the background under an `ingest*` label.
- backend regression: after cold timeout, `/api/health` does not keep
  `active_label=ingest-*` and `writer_active=true` past the SLO.
- backend integration: a synthetic slow cold archive batch yields before the
  next sub-batch, allowing a queued hot write through within the SLO.
- backend metric test: one archive sub-batch stays within the configured
  max-exec budget visible in serializer metrics.
- backend negative test: paused or shed archive repair retains backlog/retry
  intent and does not report successful ingestion for data it did not persist.
- health test: active label, active age, queue depth, archive shed, and archive
  paused/busy reason are visible.

Success criteria:

- A simulated `ingest-scan` storm leaves heartbeat/presence/runtime-live below
  the configured queue-wait SLO.
- No archive HTTP timeout can leave `active_label=ingest-scan` for multiple
  minutes on the shared serializer.

### Phase 3: Launch Wiring and Smoke

Scope:

- dogfood/local hosted launch path uses `--archive-repair-mode paused`;
- any production hosted provisioning path that installs the machine agent uses
  paused mode unless an operator explicitly opts into repair;
- server and engine status expose enough state for the menu bar and health
  surfaces to distinguish "online, archive paused" from "degraded/offline."

Smoke:

- fixture or local tenant-shaped DB with synthetic archive backlog;
- hosted `david010` smoke only after fixture success;
- verify no `ingest-scan` / `ingest-replay` labels appear during paused-mode
  smoke;
- verify managed launch reaches provider preparation instead of hanging behind
  archive writes;
- verify `/api/health` writer state returns to idle after hot writes.
- verify paused backlog can be switched to `trickle`/`drain` and make bounded
  progress, then returned to `paused`.

## Deferred Architecture

The long-term architecture is still:

```text
hot.db        small authoritative product/control state
archive/      immutable compressed raw source chunks + checksums
derived.db    rebuildable event/search/detail cache
```

That work belongs to `docs/specs/reliability-data-plane.md`. It should be
resumed after launch containment is shipped and after backup/restore gates are
settled. Reintroducing `hot.db` / `derived.db` switches in this epic would
repeat a previous mistake: scaffolding without wired runtime behavior.

## Review Gates

Before implementation:

- Hatch Opus reviews this spec.
- Spec is revised to resolve review findings.

After each committed phase:

- run focused unit/integration tests for the phase;
- ask Hatch DeepSeek to review the diff for bugs and simplification;
- fix any launch-blocking finding before moving on.

Before ship:

- full focused test set for changed engine/server paths;
- Hatch Opus final architecture review;
- Hatch DeepSeek final code review;
- exact-SHA ship and live smoke using the repo ship workflow.

## Grand Success Criteria

This epic is complete only when:

- the spec is committed and reviewed;
- hosted archive repair can be paused explicitly and defaults to paused for
  hosted/dogfood install paths;
- paused mode disables startup reconciliation, periodic fallback scan, periodic
  spool replay, and initial retry queueing;
- live shipping, runtime events, presence, heartbeat, and managed control still
  work in paused mode;
- server archive ingest cannot keep the writer slot pinned after HTTP timeout;
- server archive ingest yields between bounded cold sub-batches and re-checks
  hot pressure;
- tests cover the failure mode that caused the incident;
- Hatch Opus and DeepSeek have reviewed the final work;
- changes are merged to `main`, pushed, shipped, smoke tested, and local main is
  synced to remote main.

## What Not To Do

- Do not solve this by increasing HTTP timeouts.
- Do not solve this by running `VACUUM` or compaction on the live tenant.
- Do not solve this by hiding menu-bar red/yellow status.
- Do not solve this by disabling live shipping.
- Do not rely on priority queueing as preemption.
- Do not add a second serializer and claim launch isolation while both lanes
  still write the same SQLite file and one cold statement can monopolize the
  database.
- Do not move to Postgres just to avoid making archive repair explicit.

## Open Follow-Ups

- How should the hosted operator trigger bounded archive repair windows?
- What UI copy should represent "archive paused, live sync healthy"?
- Which exact storage and restore gates unblock the physical data-plane split?
- What exact restore drill proves paused hosted backlog survives backup/restore
  before paid launch?
- Should Starter plan limits include explicit archive backlog/storage policy
  before paid launch?
