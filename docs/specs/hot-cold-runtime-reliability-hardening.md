# Hot/Cold Runtime Reliability Hardening

**Status:** Draft, revised after Hatch Fable first-principles review
**Owner:** Longhouse core
**Created:** 2026-07-07
**Review:** Hatch Fable architecture review, 2026-07-07
**Related:** `docs/specs/hot-cold-ingest-isolation.md`,
`docs/specs/reliability-data-plane.md`, `docs/specs/archive-backlog-repair.md`,
`docs/specs/machine-control-truth.md`

## Goal

Longhouse must stay launchable, steerable, and honest while the durable archive
is slow, backlogged, rebuilding, or temporarily wedged.

The product promise for launch is not "archive repair is always caught up." The
launch promise is:

- live sessions are visible quickly;
- managed sessions can be launched and controlled quickly;
- user input and machine-control commands can be accepted quickly;
- durable transcript history eventually becomes correct, ordered, searchable,
  and replayable;
- the UI and health surfaces tell the truth when archive durability is behind.

If cold archival work can still block managed launch, remote control, runtime
state, local health, or service readiness, then the hot/cold split is not done.

## Why This Exists

The 2026-07-07 hosted dogfood incident showed that the deployed hot/cold design
is directionally correct but not yet a hard failure-domain split.

The Live Store was present and useful: runtime facts, heartbeat stamps, input
receipts, machine-control operations, and launch readiness now have a small hot
SQLite lane separate from the large durable archive. That helped keep parts of
the Runtime Host responsive.

The failure was that the cold archive writer was still treated as a global
dependency. A background archive scan wedged the archive `WriteSerializer`, and
that cold stall propagated into `readyz`, managed local launch, heartbeat
bookkeeping, archive outbox drain, and retry/backpressure behavior.

The question for launch is therefore not "is hot/cold a bad design?" The sharper
question is:

> Is the hot lane authoritative enough for live product behavior, or is it still
> an optimization layered on top of a cold archive dependency?

For launch it must be authoritative enough.

## Why The Prior Mitigation Did Not Save Us

`docs/specs/hot-cold-ingest-isolation.md` already identified the same core
mechanical failure: cold archive repair can monopolize the shared writer, and
priority cannot preempt already-running SQLite work.

The 2026-07-07 incident means one or more of the prior assumptions failed:

1. Hosted repair was not actually paused/trickled in the deployed configuration,
   or startup reconciliation still bypassed the intended pause gate.
2. Archive admission limits existed before enqueue, but a permitted
   `ingest-scan` unit could still run for tens of minutes once admitted.
3. Bounded chunking did not cover every expensive archive stage or SQLite query.
4. Timeout/backpressure did not provide a kill path for an already-running
   archive writer thread.
5. Health still treated cold writer stall as total service unavailability, so
   the system recovered only when the container restarted.

The first implementation task is therefore not new feature work. It is an audit:
prove exactly which prior guard failed, add a regression test for it, and only
then build the broader split.

## Current Topology

### Hot Lane

The hot lane is a separate Live Store schema with its own writer and compact
tables for live product facts:

- `LiveRuntimeState`
- `LiveControlLease`
- `LiveLaunchReadiness`
- `LiveMachineControlOperation`
- `LiveSessionInputReceipt`
- `LiveHeartbeatStamp`
- `LiveArchiveOutbox`

Runtime events now write live state first and enqueue archive provenance through
the Live Store outbox.

Heartbeat requests can write a hot heartbeat stamp before scheduling cold
bookkeeping.

Remote launch has a hot `LiveLaunchReadiness` projection, which is the right
shape for launch state that should survive archive pressure.

### Cold Lane

The cold lane is the durable archive SQLite database and its single
`WriteSerializer`. It owns:

- durable `AgentSession` rows and kernel rows;
- source lines, events, turns, branches, chunks, and archive projections;
- FTS/search/detail state;
- legacy launch/session input rows still used by some product flows;
- background repair, reconciliation, replay, summary, and projection work.

### Bridge

Hot facts that need durable provenance are bridged through
`LiveArchiveOutbox`. The maintenance loop drains that outbox by entering the
cold archive `WriteSerializer` under the `live-archive-drain` label.

This is the right bridge concept, but it needs stricter pressure semantics:

- outbox backlog is acceptable;
- outbox drain is opportunistic;
- outbox drain must not compete with hot control;
- outbox drain must not make the Runtime Host unhealthy while the hot lane is
  healthy.

## Initial Numeric Budgets

These are launch defaults to make "bounded" falsifiable. They can be tuned from
metrics, but implementation must not ship with adjectives alone.

- Hot route queue wait target: p95 under 250 ms, p99 under 1000 ms during
  synthetic cold pressure.
- Cold archive writer active warning: 10 seconds.
- Cold archive writer stale threshold: 30 seconds.
- Single archive ingest/replay unit: target under 1000 ms, hard failure over
  5000 ms in synthetic tests.
- Archive sub-batch: at most 64 logical items or 256 KiB decoded payload,
  whichever comes first, until tenant metrics prove a larger cap is safe.
- WAL warning: 512 MiB.
- WAL archive-shed threshold: 1 GiB.
- WAL emergency archive-pause threshold: 2 GiB.
- Live archive drain: at most one batch per 10-second tick by default; batch cap
  25 rows or 1000 ms, whichever comes first.
- Guaranteed drain share: when cold writer is healthy and WAL is below warning,
  attempt at least one `live-archive-drain` batch per 60 seconds even if other
  archive backlog exists.
- Undrained outbox rows are never deleted automatically. Alert at oldest
  pending over 10 minutes, critical at over 1 hour or over 100k pending rows.
  If critical, shed non-critical heartbeat/runtime archive provenance before
  shedding input/control receipts.

## Incident Evidence

Observed hosted dogfood symptoms:

- `/api/readyz` returned `503` with `reason=write_serializer_stalled`.
- Cold writer `active_label` was `ingest-scan`.
- Cold writer active age reached roughly 46 minutes.
- Cold writer queue depth exceeded 1000.
- Queued work included presence, runner, runtime, heartbeat, and control
  follow-up labels.
- The archive DB was roughly 125 GB and the WAL grew beyond 3 GB while
  checkpoints reported busy.
- A Python worker thread was in disk sleep and had performed tens of GB of
  SQLite read/write IO.
- Managed local launch returned "database writer stalled" because launch still
  checks the cold writer before dispatch.
- A container restart cleared the in-memory wedged writer and made health green,
  but did not remove the backlog or the coupling.

This is enough to classify the incident as cold archive writer starvation with
hot product blast radius. It is not primarily provider latency, menu-bar UI
confusion, or a one-off deploy failure.

## Root Diagnosis

### 1. The hot lane is real but incomplete

The Live Store moved important fast facts out of the large archive DB, but not
all launch-critical behavior has moved behind the hot boundary.

Known remaining cold dependencies:

- managed local launch checks the cold writer and blocks when it is stale;
- `readyz` fails the whole service when the cold writer stalls;
- heartbeat still schedules `heartbeat-bookkeeping` on the cold writer;
- `LiveArchiveOutbox` drains through the cold writer;
- remote launch still creates durable archive session/kernel rows in the main
  request after writing hot readiness;
- some late machine-control reconciliation still falls back to cold rows;
- archive reconciliation/replay can still enqueue pressure faster than the cold
  lane can safely drain.

### 2. Priority is not preemption

`WriteSerializer` priority only chooses the next queued item. It cannot preempt
an already-running SQLite task.

Once `ingest-scan` owns the cold writer, priority-zero launch/control work can
only wait, shed, or fail. That is why cold archive repair can still take the
product hostage.

### 3. Timeout is not cancellation

Python cannot safely kill a worker thread blocked in SQLite. Returning a timeout
to the caller while the worker continues still leaves the writer active until
the underlying work finishes.

Therefore the system must prevent unbounded cold work from entering the writer
in the first place. The only reliable preemption unit is a small, cooperative
archive sub-batch that returns quickly.

### 4. Archive backlog is recoverable; live control outage is not

For launch, a stale archive is a degraded indexing state. A blocked launch or
blocked input path is a product outage.

The architecture must encode that priority directly. Health, admission control,
and UI copy should all treat live control as the product-critical lane and
archive catch-up as background durability work.

### 5. Health currently conflates cold and hot failure

`readyz` currently returns 503 for a stale cold `WriteSerializer` even when the
hot writer is healthy. That is appropriate only if the archive writer is still a
hard dependency for product readiness.

After hot/cold isolation, readiness should distinguish:

- `ready`: hot lane can accept live/control work;
- `archive_degraded`: durable archive is behind or stalled;
- `not_ready`: hot lane or core dependency is unavailable.

## Product Rule

Hot product behavior must not depend on cold archive progress.

Hot behavior:

- managed launch;
- remote steer/input;
- machine-control command dispatch and reconciliation;
- runtime state;
- control leases;
- live session previews;
- heartbeat freshness;
- local health/menu-bar status;
- active/timeline card state for recent live sessions.

Cold behavior:

- source-line/event archive persistence;
- branch/source reconstruction;
- replay and reconciliation scan;
- FTS/search/detail rebuild;
- summary/title enrichment;
- historical backfill;
- Live Store outbox drain into archive.

Cold behavior may lag, pause, retry, shed, or report degraded. It must not make
hot behavior unavailable.

## Required Launch Architecture

### Hot lane as live authority

For live product behavior, the hot lane is the authority. The archive is the
durable record, not the synchronous gate.

This implies:

- a managed launch can be created, dispatched, and shown as pending/running from
  hot state even if archive projection is delayed;
- an input receipt can be accepted from hot state even if durable
  `SessionInput` projection is delayed;
- runtime state can advance from hot state even if archive runtime events are
  queued;
- machine-control command results can reconcile hot operations without waiting
  for cold rows;
- the UI can show "archive catching up" instead of treating the session as
  missing or failed.

### Archive as eventually durable

Archive correctness still matters. The hot lane is not permission to lose data.

Required archive semantics:

- hot fact plus outbox enqueue must commit atomically in the Live Store;
- every hot fact that needs durable provenance enters an idempotent outbox;
- outbox rows survive restart;
- archive projection is idempotent;
- archive projection can be retried later;
- backlog age/count/failure is visible;
- archive lag is represented as a product state, not hidden.

### Health split

`readyz` should answer whether the Runtime Host can serve live product behavior.

Proposed health taxonomy:

- `ready`: hot writer healthy, DB connections available, control dispatch
  available.
- `ready_with_archive_degraded`: hot path healthy, cold archive writer stale,
  WAL pressure high, archive outbox old, or backlog paused.
- `not_ready`: hot writer stale, Live Store unavailable when configured,
  required auth/config unavailable, or core process cannot serve requests.

Operator/deep health should still expose cold archive failure loudly. The change
is not to hide archive degradation; it is to stop letting archive degradation
masquerade as total product unavailability.

### Cold writer kill path

Prevention is not enough. If a cold SQLite unit wedges despite admission and
budgeting, the system needs a recovery path that is not "hope Docker notices."

Launch requirement:

- add a synthetic cold-stall harness first;
- add an explicit archive-degraded health signal separate from hot readiness;
- add an operator-visible recovery action that pauses archive repair and
  restarts only the archive worker if possible;
- timebox a supervised child-process archive writer spike to one day.

Preferred shape:

- cold archive ingest/replay/drain runs in a supervised child process;
- the parent process owns the hot Runtime Host and Live Store;
- if cold archive work exceeds the hard active-age threshold, the parent marks
  archive degraded, kills the child, and restarts it with archive repair paused
  or trickled;
- hot readiness remains based on the parent and Live Store.

Fallback if child-process isolation is not ready for launch:

- document an explicit operator remediation: set archive repair to paused,
  restart the Runtime Host, verify hot readiness, then re-enable archive trickle
  only after WAL and queue depth are below thresholds;
- keep a separate archive health endpoint/alert that can page on a cold wedge
  even when `/api/readyz` stays hot-ready.

The spec is not complete without either the child-process path or the documented
fallback.

### Hot read overlay

Write isolation alone is insufficient. Launch and control also need read-side
visibility when archive projection lags.

Required read behavior:

- recently launched/running sessions can render from hot launch/runtime/control
  state before durable `AgentSession` projection completes;
- timeline/session cards can show a hot overlay with
  `archive_projection=pending`;
- cold transcript/search/detail reads may show "archive catching up" instead of
  claiming the live session does not exist;
- read paths that still require archive state must degrade explicitly.

Acceptance:

- with archive projection blocked, a newly launched managed session appears in
  the active/timeline surface from hot state;
- once projection completes, the hot overlay merges into the durable archive row
  without duplicate session cards.

## Required Engineering Changes

### 1. Make managed launch hot-first

Current problem:

- managed local launch explicitly blocks on the cold writer when it is stale.

Required shape:

- session identity is minted before either hot or cold projection and is reused
  by engine ingest, live runtime, control operations, and archive projection;
- launch intent is recorded in hot state first;
- command dispatch proceeds from hot state;
- archive session/kernel projection is attempted opportunistically;
- if archive projection is delayed, the session remains visible as hot
  pending/running with `archive_projection=pending`;
- archive projection eventually creates/updates durable `AgentSession`,
  thread/run/attempt, and launch provenance rows idempotently.

Acceptance:

- with a synthetic cold writer stall, managed launch still creates a hot launch
  intent and reaches command dispatch;
- the UI shows the session as launching/running from hot state;
- archive catch-up later creates the durable session without duplicate sessions.
- engine-shipped source lines/events bind to the same session id even if archive
  session projection is delayed.

### 2. Make control and input hot-first

Current problem:

- live input receipts exist, but some paths still create or update cold
  `SessionInput` rows synchronously when hot receipt is missing or after
  dispatch;
- late command reconciliation can fall back to cold machine-control rows.

Required shape:

- hot receipt/operation is the primary synchronous acknowledgement;
- durable archive input/provenance rows are outbox projections;
- delivery can proceed from hot state;
- cold projection failures do not change the hot delivery outcome.

Acceptance:

- user input can be accepted and dispatched while archive writer is stale;
- duplicate `client_request_id` is resolved from hot state;
- delayed archive projection is idempotent.

### 3. Make heartbeat hot-only on the request path

Current problem:

- heartbeat writes a hot stamp, then schedules cold `heartbeat-bookkeeping`;
- cold bookkeeping has shown multi-second normal executions and long outliers.

Required shape:

- request path writes hot heartbeat/lease state only;
- cold heartbeat archive/provenance is outbox-only;
- expensive session lifecycle reconciliation is bounded, debounced, and
  interruptible between units;
- heartbeat freshness/local health does not depend on cold writer health.

Acceptance:

- heartbeat request latency remains bounded during cold archive pressure;
- stale cold writer does not make machines appear offline if hot heartbeats are
  current;
- lifecycle close/open projections eventually reconcile without blocking
  heartbeat freshness.

### 4. Make Live Archive Outbox strictly opportunistic

Current problem:

- outbox drain enters the cold `WriteSerializer` and may compete with archive
  ingest/replay and other cold work.

Required shape:

- drain only when cold writer is idle/healthy and WAL pressure is below the
  threshold;
- drain also has a guaranteed minimum share when cold writer is healthy, so
  outbox durability cannot starve forever behind lower-value archive repair;
- drain uses small bounded batches with per-tick execution budgets;
- drain never waits behind a stale cold writer;
- drain skips and reports lag rather than increasing hot pressure;
- outbox lag affects archive health, not hot readiness.

Acceptance:

- with cold writer stale, outbox pending count grows but hot routes stay
  healthy;
- drain resumes after cold writer recovery;
- outbox rows do not duplicate archive facts across retries.
- hot DB/outbox growth has alerts and a non-destructive critical-state policy.

### 5. Bound archive ingest and repair

Current problem:

- `ingest-scan` can enter a long SQLite task that cannot be preempted;
- engine retries/reconciliation can wake large backlogs;
- archive admission helps before enqueue, but cannot release an already-running
  cold writer.

Required shape:

- archive repair is paused/trickle/drain with explicit mode and status;
- hosted defaults to paused or trickle until the tenant proves it can drain
  safely;
- every archive write unit has concrete row/byte/time budgets;
- archive requests yield between sub-batches and re-check pressure;
- no archive timeout path leaves a background `ingest-*` writer active;
- broad repair scans use adaptive backoff from server pressure headers.

Acceptance:

- no single `ingest-scan` or `ingest-replay` unit can exceed the cold writer
  execution budget under synthetic tests;
- a scan storm cannot keep the cold writer active for minutes;
- backlogged archive work is visible and drainable later.

### 6. Add cold DB guardrails

Required guardrails:

- WAL byte threshold warnings and hard archive-shed threshold;
- cold writer active-age alert;
- cold queue-depth alert;
- archive outbox oldest-pending alert;
- disk IO pressure signal in hosted diagnostics;
- no corpus-wide ad hoc DB scans in request paths or normal diagnostics;
- operator-only diagnostic commands must be bounded or explicitly marked
  dangerous.

Acceptance:

- system reports archive pressure before `readyz` failure;
- diagnostics do not create enough IO pressure to worsen an incident;
- launch smoke includes a tenant-sized synthetic archive pressure test.

### 7. Add exact stuck-stage observability

Current problem:

- `readyz` can say `active_label=ingest-scan`, but not the exact stage inside
  archive ingest.

Required shape:

- `WriteSerializer` exposes active label, age, queue depth, and active stage;
- archive ingest updates stage before expensive units;
- stage updates include session id, operation kind, row counts, byte counts,
  and last progress timestamp where safe;
- operator endpoint or signal can capture in-process Python stacks;
- cold archive and hot writer metrics are separate.

Acceptance:

- next stall report can say "stuck in archive chunk overlap lookup" or
  equivalent, not just "stuck in ingest-scan";
- stack capture exists without attaching an external debugger;
- alerts route hot outage vs archive degradation differently.

## Bad Design Check

The concerning design is not "SQLite exists" or "hot/cold exists."

The concerning design would be:

- hot/cold adds more tables but does not change failure domains;
- live product flows still synchronously require archive projection;
- health still treats archive backlog as total service failure;
- background archive repair still launches automatically and monopolizes the
  archive writer;
- the UI hides archive lag until users see broken launch/control behavior.

The good design is:

- hot lane owns live product behavior;
- cold lane owns durable archive correctness;
- an explicit, idempotent bridge connects them;
- archive lag is visible and recoverable;
- archive failure cannot block launch/control/input.

This is falsifiable. If we can stall the cold archive writer in a test and
managed launch/input/runtime still work, the design is sound enough for launch.
If not, hot/cold is ceremony without isolation.

## Launch Acceptance Tests

Minimum must-pass tests:

- prior-mitigation regression:
  - deployed hosted/dogfood config defaults archive repair to paused or trickle;
  - startup reconciliation cannot bypass the configured mode;
  - no `ingest-scan` runs while repair mode is paused.
- synthetic cold writer stall while Live Store is healthy:
  - `/api/readyz` returns ready or ready-with-archive-degraded;
  - managed launch reaches dispatch;
  - user input receipt is accepted;
  - runtime state updates are accepted;
  - heartbeat freshness remains current;
  - archive health reports degraded.
- cold writer kill path:
  - once cold writer active age exceeds the hard threshold, archive health
    reports the wedged unit;
  - child-process isolation kills/restarts the archive worker, or the documented
    operator fallback pauses repair and recovers hot readiness after restart.
- synthetic archive scan storm:
  - no `ingest-scan` unit exceeds the configured execution budget;
  - hot writer queue wait remains under the hot SLO;
  - archive requests receive typed backpressure and retry-after.
- outbox lag:
  - outbox rows persist across restart;
  - drain resumes idempotently after cold recovery;
  - UI/health surfaces expose lag without reporting live control down.
- WAL pressure:
  - archive drain and scan pause at threshold;
  - hot lane remains usable;
  - operator health explains the dominant pressure signal.
- restart recovery:
  - restart is not the primary recovery mechanism;
  - after restart, archive repair does not immediately recreate the same stall;
  - mode/backoff survives restart.

## Phased Plan

### Phase 0: Review and prior-mitigation audit

- Finish this synthesis.
- Get first-principles review from an independent agent.
- Revise the spec for valid pushback.
- Audit why the 2026-06-29 ingest-isolation spec did not prevent the 2026-07-07
  incident.
- Add a regression test for the exact failed guard.

### Phase 1: Cold-stall harness, health, and observability

- Build the synthetic cold-stall harness first and capture current baseline
  failures.
- Split hot readiness from archive degradation.
- Add active-stage reporting.
- Add archive pressure metrics/alerts.
- Make diagnostics bounded.
- Decide the HTTP contract for hot-ready/archive-degraded health before
  changing orchestration behavior.

This phase makes the system honest before deeper behavior changes.

### Phase 2: Hot-first managed launch

- Remove cold-writer stale preflight as a launch blocker.
- Move launch execution off the cold writer, not just the preflight.
- Write hot launch intent before archive projection.
- Define session identity and projection idempotency rules.
- Project launch/session rows to archive idempotently.

This phase protects the product wedge: launch and steer.

### Phase 3: Hot-first input, heartbeat, and control reconciliation

- Treat live input receipts as primary synchronous receipts.
- Move request-path heartbeat truth fully to hot state.
- Debounce or outbox cold bookkeeping.
- Keep machine-control reconciliation hot-first.

This phase protects liveness and menu-bar truth.

### Phase 4: Archive repair containment

- Hosted archive repair defaults to paused/trickle.
- Bound archive ingest units.
- Make outbox drain opportunistic and pressure-aware.
- Add guaranteed drain share and hot DB/outbox critical-state policy.
- Add engine adaptive backoff from typed server pressure.
- Timebox child-process archive writer spike; implement if tractable, otherwise
  document the launch fallback.

This phase prevents recurrence.

### Phase 5: Chaos and dogfood proof

- Run cold-writer-stall and archive-scan-storm tests.
- Run hosted dogfood smoke with backlog present.
- Prove launch/input/runtime/heartbeat survive archive degradation.
- Only then call the hot/cold design launch-ready.

## Non-Goals

- No Postgres requirement for core Runtime Host.
- No Kafka/Redis/external queue requirement for core.
- No hiding archive degradation from operators or users.
- No weakening durable transcript correctness.
- No live `VACUUM` or destructive compaction as an incident fix.
- No broad rewrite of the archive data model before launch unless the acceptance
  tests prove the current architecture cannot be isolated.

## Open Questions

- Which routes should return `ready_with_archive_degraded` versus 200 with a
  health body versus 503?
- What exact UI copy represents "live control healthy, archive catching up"?
- What is the max acceptable hot receipt/outbox retention if archive is down for
  hours?
- Should hosted paid launch default archive repair to paused, trickle, or an
  adaptive mode?
- What archive DB size/IO profile forces a post-launch move from SQLite archive
  to object chunks plus rebuildable derived indexes?
- Which exact cold archive stages need active-stage labels first?
- Can the archive writer move to a supervised child process inside the launch
  window?
- What is the exact durable ordering rule when hot launch identity exists before
  cold `AgentSession` projection?

## Decision

The deployed hot/cold split should be kept and completed, not abandoned.

But the launch bar is strict: hot/cold only counts as robust when cold archive
failure no longer blocks the live product loop.

If the synthetic cold-stall test still blocks launch/input/runtime after Phase
2, the design must be treated as incomplete ceremony and rethought before paid
launch.
