# Prelaunch Runtime Hardening Wins

**Status:** Draft for architecture refinement
**Owner:** Longhouse core
**Created:** 2026-07-08
**Related:** `docs/specs/hot-cold-runtime-reliability-hardening.md`,
`docs/specs/hot-cold-ingest-isolation.md`,
`docs/specs/reliability-data-plane.md`,
`docs/specs/archive-backlog-repair.md`

## Context

The July 8 hosted dogfood outage was a cold archive failure with live-product
blast radius.

The immediate fix removed a full `events_fts` rebuild from subagent relink and
restarted the hosted tenant after reclaiming disk. The long-term fix is stricter:
live launch/control/readiness must not share failure fate with archive indexing,
repair, or search maintenance.

This document is the launch-practical hardening pass: highest-benefit work first,
with simplifications called out explicitly. It does not replace the broader
hot/cold data-plane specs. It narrows the next build loop.

## External Research Notes

Exa deep-reasoning research on SQLite FTS5/WAL and small-system ingest patterns
reinforced these rules:

- FTS5 `rebuild` discards and rebuilds the whole index. It is a repair/startup
  tool, not live ingest maintenance.
- FTS5 `optimize`, `automerge`, and `crisismerge` can make an individual write
  unexpectedly expensive. Use them only in explicit maintenance windows or
  coarse background loops with metrics.
- WAL improves reader/writer concurrency, but one writer still owns commit
  progress. Long readers can starve checkpoints and grow WAL until disk becomes
  the outage.
- Treat SQLite as a serialized sink. Parallelize parsing and preparation, then
  admit bounded writes through a small number of explicit lanes.
- A queue in the same SQLite database is fine at Longhouse launch scale if it has
  atomic claim, visibility timeout/reaper, dead-letter semantics, and observable
  lag. Do not add Redis/Kafka just to paper over unbounded write units.
- Archive-first systems stay resilient by keeping raw durable evidence separate
  from derived indexes. Search/detail/summary are rebuildable projections.

## Launch Rule

Longhouse can launch with archive lag. It cannot launch with archive lag making
Helm, Console, heartbeat, readiness, or the menu bar look dead.

Every runtime path must be classified as:

- **live authoritative:** launch, send, interrupt, runtime state, heartbeat,
  input receipts, current transcript preview, readiness;
- **durable archive:** raw source/events/source lines/session detail/search;
- **derived projection:** FTS, recall chunks, summaries, embeddings, timeline
  card enrichment, diagnostics.

Only live-authoritative work may decide whether the service is up.

## Highest-Benefit Wins

### Landed: cold writer wedges degrade by default

The archive `WriteSerializer` no longer calls `os._exit(86)` by default.
Live/control writers may still fail fast when they are genuinely wedged.

Implemented:

- archive serializer name: `archive`
- archive env override:
  `LONGHOUSE_ARCHIVE_WRITE_SERIALIZER_EXIT_ON_WEDGED_WRITER`
- live serializer name: `live`
- live env override:
  `LONGHOUSE_LIVE_WRITE_SERIALIZER_EXIT_ON_WEDGED_WRITER`
- health already projects archive stalls as `degraded` /
  `ready_with_archive_degraded` when the active label is archive-degradable.
- tests prove archive deadman cleanup releases the writer slot without process
  exit, retains wedged evidence, and lets the live serializer proceed while the
  archive serializer is stuck.

Why this is high value:

- A cold archive worker can still be pinned by Python or SQLite work that cannot
  be interrupted immediately.
- Restarting the process should be an operator or supervisor decision based on
  live-lane health, not the archive writer's first self-diagnosis.
- This turns a whole-host outage into explicit archive degradation.

Remaining acceptance:

- live writer wedge still has fail-fast behavior outside tests;
- `/readyz` stays ready-with-degraded for archive labels and 503s for live labels;
- logs include serializer name and wedged label.

### Landed: ban whole-index FTS work from request and ingest paths

`events_fts` rebuilds are allowed only at startup initialization when the FTS
objects are missing/inconsistent, and through explicit repair APIs.

Implemented:

- `relink_orphan_subagents_for_parent` no longer runs a full FTS rebuild.
- A source-level test asserts the `events_fts` rebuild SQL appears only in
  `database.py` and the explicit `AgentsStore.rebuild_fts()` helper.
- Another test asserts relink does not issue rebuild SQL.

Next refinements:

- Replace `AgentsStore.rebuild_fts()` call sites with explicit command naming
  before adding any new callers. New callers must edit the source-level allow
  list test on purpose.
- Add operator docs: `rebuild` is repair, `optimize` is maintenance, normal
  ingest is trigger/incremental.
- Health should expose the last FTS rebuild/repair result if a repair command
  ever runs in hosted.

Acceptance:

- no router, ingest service, kernel backfill, or maintenance tick may issue
  `INSERT INTO events_fts(events_fts) VALUES('rebuild')`;
- FTS repair commands require an explicit operator action and emit duration,
  rows, WAL bytes before/after, and failure detail.

### 1. Keep archive repair explicit, not generic-infra

**Decision:** Do not build a generic archive-maintenance ledger before launch.
That is too much infrastructure for the immediate risk. Build only explicit,
operator-triggered repair lanes for the work that can take down the archive
writer, starting with FTS repair.

Do not put more recovery logic inside request handlers. Request handlers may:

- validate and append raw/live facts;
- enqueue a bounded outbox or explicit repair row;
- return with honest state.

They may not:

- run full scans;
- rebuild indexes;
- compact/optimize;
- drain historical backlog until empty;
- hold the archive writer while also waiting on network, provider, or UI state.

Simplification:

- One `fts_repair` state row is enough prelaunch: state, started/completed time,
  attempt count, last error, rows touched, WAL bytes before/after, and duration.
- Leave projection/enrichment loops separate unless they are proven to threaten
  live behavior. Freeze broad scheduler unification.

Acceptance:

- FTS repair is operator-triggered and restart-visible;
- no request handler starts FTS repair;
- repair emits metrics and final state into trusted health;
- failed repair leaves the system live with search/indexing degraded.

### Landed: make WAL pressure a first-class shed signal

**Decision:** WAL growth is not just a DB metric; it is a control input.

Current implementation exposes WAL bytes and checkpoint metrics, and archive
admission now uses archive WAL size as a shed signal. Start with two states
before adding a ladder nobody has operated:

- ok: archive replay/scan may proceed under existing limits;
- shed: reject archive replay/scan with typed backpressure until WAL drains.

Initial threshold should stay conservative and configurable:

- archive shed: 1 GiB;
- live WAL warning should be much lower, currently 64 MiB scale.

Simplification:

- Do not add another bespoke "DB pressure" service. Reuse trusted health metrics
  and one admission helper that returns a typed pressure decision.

Acceptance:

- archive admission checks WAL bytes before accepting replay/scan work;
- live/control routes do not shed solely because archive WAL is large;
- WAL checkpoint `busy` and `remaining_frames` are visible in trusted health;
- shed decisions are visible in trusted health and `/readyz` with the triggering
  WAL value;
- admission recovers automatically when WAL drains below threshold.

### 3. Make hosted restart default conservative

**Decision:** Hosted runtime and Machine Agent restarts must not silently resume
full archive drain.

Prelaunch scope:

- hosted dogfood default is `paused` or `trickle`, never unbounded `drain`;
- Runtime Host returns typed archive backpressure with `Retry-After`;
- if a mode header is added, the engine may use it only to slow replay pacing.

Simplification:

- One archive mode enum: `paused | trickle | drain`.
- Freeze full mode propagation into local health until after launch unless it is
  needed to stop replay storms.
- Do not delete parallel per-path flags during the launch hardening loop; first
  make the hosted default safe.

Acceptance:

- hosted dogfood defaults to `paused` or `trickle`, never unbounded `drain`;
- after Runtime Host restart, archive replay admission rate stays below a small
  configured ceiling until the host reports non-degraded;
- mode/backpressure transitions are logged on both sides.

### 4. Keep hot reads off derived/archive stores

**Decision:** Timeline list, session cards, launch state, local health, and
readiness read small hot/projection tables only.

Simplification:

- If a card needs expensive derived detail, render an explicit partial state
  instead of reaching into source lines/events at request time.
- Treat missing derived rows as "indexing delayed," not as a reason to run
  repair in the read request.

Acceptance:

- add cheap source/query guards for the two or three hot routes currently
  suspected of touching raw archive, FTS, or source-line tables;
- trusted health exposes derived lag separately;
- UI copy names the degraded axis: archive, search, projections, or live.

### 5. Add the July 8 regression scenario

**Decision:** The launch gate needs one end-to-end degradation test that
recreates the class of outage, not just unit pins.

Scenario:

- archive writer is wedged or WAL pressure is above shed threshold;
- archive ingest/replay is rejected or delayed with typed backpressure;
- live launch, send/interrupt where possible, heartbeat, runtime state, and
  `/readyz` remain healthy or explicitly ready-with-archive-degraded.

Acceptance:

- the scenario runs without sleeping for real minutes;
- assertions cover both health status and user-visible live operations;
- failure output names whether the live lane, archive lane, or health
  classification regressed.

## Deletions And Freezes

These are the simplifications with the best launch leverage:

- Freeze automatic full-history repair on hosted until operator-triggered.
- Delete hidden fallback paths that switch from hot/live behavior into archive
  catch-up during a user request.
- Remove duplicated archive mode flags in favor of one mode enum.
- Stop treating `needs_user` freshness as health. Process/runtime truth should
  close lifecycle; attention state is a UI hint.
- Keep `PRAGMA optimize` as explicit/coarse maintenance. Do not sprinkle it into
  write paths.
- Avoid new storage systems before launch. SQLite is acceptable if work units are
  bounded and live/archive fate is split.
- Freeze generic archive job-ledger work until after launch. Build only the
  single-purpose FTS repair state if needed.
- Keep archive-primary fallback frozen, not deleted, until legacy raw fallback is
  no longer load-bearing.

## Implementation Order

1. Done: land archive/live writer exit-policy split and FTS rebuild guard.
2. Add the July 8 degradation regression scenario.
3. Done: wire one WAL shed threshold into archive admission and trusted health.
4. Change hosted archive restart default to paused/trickle, with replay rate
   ceiling and transition logs.
5. Add one explicit FTS repair command/state row only if hosted repair needs it.
6. Add cheap route-specific guards for suspected hot archive reads.
7. Add two operator commands only:
   - pause/resume/trickle archive work;
   - run FTS repair with dry-run and metrics.

## Closed Review Decisions

- Generic archive ledger: no prelaunch. Single-purpose FTS repair state only.
- Hosted archive writer self-exit: no by default. Supervisor restart should be
  driven by live-lane health; env override remains an escape hatch.
- Engine protocol: smallest viable change is typed backpressure plus
  `Retry-After`, optionally one mode header for replay pacing.
- Archive-primary fallback: keep frozen. Do not delete before launch.
- Hot-route dependency framework: no generic framework prelaunch; add cheap
  guards only where there is concrete suspicion.
