# Local Truth Projection

Status: Proposed implementation plan, Fable-reviewed
Last updated: 2026-07-16
Owner: Machine Agent / macOS ambient product
Related: `macos-menu-bar-state-model.md`, `macos-launch-product-shape.md`,
`managed-provider-session-contract.md`, `speed-of-light-shipper.md`

## Executive Summary

The Longhouse menu bar now presents local state honestly, but the Machine Agent
still produces that state through repeated broad discovery. Every nominal
one-second status refresh rereads historical managed-provider state, runs
multiple process inventories, probes provider servers, rebuilds the complete
projection, and only then writes `engine-status.json`.

That architecture makes a small status surface depend on work proportional to
historical residue rather than current sessions. On the dogfood Mac, 66 managed
state files currently project to five live managed sessions. Managed scans take
roughly 0.8–2 seconds in normal operation and can exceed six seconds; unmanaged
binding reconciliation can take tens of seconds. The engine can consume most of
a CPU core while the status file still advances only every four to six seconds.

The correction is to make local status a projection, not a scan result. The
smallest design is sufficient:

- retain the latest coherent provider observations in the daemon's existing
  loop-local state;
- write status from those observations without waiting for the next scan;
- make scans producers of updated evidence, not owners of publication;
- share expensive process/probe evidence within each reconciliation pass;
- keep cold/broad scans for startup, wake recovery, and bounded reconciliation;
- separate engine liveness, projection generation time, and evidence age;
- add state-directory watchers only if the measured bounded scan cadence cannot
  meet the product target.

This is an execution-path correction. It does not reopen the shipped menu-bar
information architecture and does not introduce another durable source of
session truth.

## Product Job

The local truth path must answer, immediately and honestly:

1. Is the Machine Agent alive?
2. Which Longhouse-owned sessions and control paths are alive on this machine?
3. How recently was each claim observed?
4. Is the engine reconciling after startup, wake, or an observation failure?
5. Does the user need to act?

The menu bar, `longhouse local-health`, Runtime Host heartbeat, doctor, and
future native app surfaces should consume the same projection. None should
independently rediscover provider processes or reinterpret historical state.

## Current Evidence

Dogfood evidence captured on 2026-07-16:

- installed CLI and engine: `0.1.28-dev+cc99195d`;
- archive repair: complete, zero pending ranges, bytes, and dead ranges;
- five attached managed sessions and no current orphan bridge warning;
- 25 Codex bridge files, 39 OpenCode bridge files, one Claude channel file,
  and one Cursor Helm file;
- only three Codex bridge pids, one OpenCode server pid, and one Claude pid
  currently exist;
- recent managed scan warnings averaged about 1.4 seconds and reached about
  six seconds;
- recent unmanaged binding refresh warnings averaged about 1.8 seconds and
  reached about 48 seconds;
- observed `engine-status.json` cadence under active load: four to six seconds,
  despite a nominal one-second local-status interval;
- observed engine CPU during inspection: roughly 56–90% of one core;
- Python `longhouse local-health --fast --json`: about 440 ms on warm dogfood;
- native `longhouse-engine device local-health --json`: below the measurement
  resolution of `/usr/bin/time` for the same status file.

These measurements are diagnostic evidence, not permanent benchmark constants.
The acceptance gate below defines the lasting contract.

## Root Cause

### Historical discovery runs at presentation cadence

`engine/src/daemon.rs` starts a managed observation scan on every local-status
timer tick. The scan:

- invokes the Codex bridge collector;
- invokes the Claude channel collector;
- invokes the OpenCode server collector;
- invokes the Cursor Helm collector;
- rereads every state file found in each provider directory;
- performs multiple independent process inventories;
- checks process and attachment liveness;
- performs OpenCode health requests for viable server pids.

Only after that blocking task joins does the event loop rebuild the heartbeat
payload and write `engine-status.json`.

The unmanaged binding path separately discovers recent transcript files, runs
another process inventory, and calls `lsof` for provider processes. It runs
every 30 seconds even when it emits zero unmanaged bindings.

### The periodic heartbeat repeats scans on the event loop

The five-minute server-heartbeat timer calls all four managed observation
collectors synchronously inside the async daemon event loop. A multi-second
scan at that call site delays live shipping joins, transcript wake handling,
control-channel work, and every other event-loop arm. Fixing only the
one-second scan task would leave this second blocking path intact.

Every heartbeat and local-status publication must consume retained projection
state. No provider discovery or health probe may run synchronously on the async
event loop.

### The current status writer contains hidden discovery and enrichment

`write_local_status_snapshot` is not currently a cheap serializer. It builds
leases, reads spool and phase-ledger state, applies local titles, may open the
OpenCode database for untitled sessions, and falls back to synchronous full
unmanaged discovery when no cached unmanaged binding result exists.

The decoupled writer must not inherit those costs. Projection-building work,
titles, ledger reads, and unmanaged provenance belong in producer/reconciliation
steps. If unmanaged evidence has not been collected yet, publication emits an
empty/last-known set with explicit unknown provenance; it never performs
`ps`/`lsof` discovery inline.

### Observation currently owns Claude cleanup side effects

The Claude collector reaps dead channel state files, and terminal-signal
reconciliation currently piggybacks on managed scan completion. Once scan
cadence changes, those responsibilities need explicit ownership. Claude state
reaping moves to the slow reconciliation path and preserves its current
process-scan validity check and grace period. Terminal-signal reconciliation
runs when fresh Claude evidence is accepted, not when status happens to write.

### Snapshot age conflates separate facts

The fast classifier currently derives `engine_status_aging` and
`engine_status_stale` from the status-file modification time. A delayed write
can therefore mean any of the following:

- the engine process is stopped;
- the event loop is busy doing useful live shipping;
- a provider reconciliation is slow;
- the machine just woke;
- the projection writer failed;
- the underlying evidence is genuinely old.

Those are different states with different user implications. File age alone
cannot distinguish them.

### The consumer side is already mostly correct

The macOS app does not need a new presentation architecture:

- `SnapshotStore` retains a last-good snapshot;
- `LocalStatusMonitor` watches the engine status file;
- Runtime Host session commits arrive through the canonical session stream;
- one Swift presentation reducer owns header, badge, and panel promotion.

The missing seam is an incremental producer behind the existing status
contract.

## Goals

- Make status production proportional to currently relevant sessions, not all
  historical provider state files.
- Keep the last coherent projection readable while discovery or reconciliation
  is running.
- Reflect a signaled local provider/control change in the status projection
  within 250 ms at p95 after the Machine Agent receives the signal.
- Keep an awake, idle Machine Agent below 2% sustained CPU on the dogfood Mac.
- Keep normal status publication cadence at or below two seconds while awake.
- Recover a correct projection within one second after a normal wake when no
  provider discovery is blocked; retain and label last-known facts otherwise.
- Preserve provider-specific liveness semantics and explicit control ownership.
- Make every slow stage attributable from engine telemetry.
- Keep `engine-status.json` an atomic, backward-compatible local contract during
  migration.

## Non-Goals

- Do not redesign the shipped menu-bar sections, copy, badge precedence, or
  attention colors.
- Do not add a second durable session database or local event archive.
- Do not move Runtime Host session truth into the desktop app.
- Do not make the Swift app parse launchd, provider state directories, or
  process tables.
- Do not terminate, reap, or otherwise mutate provider processes as a side
  effect of observation.
- Do not silently delete Codex/OpenCode state needed for diagnosis, explicit
  stop, or future repair.
- Do not weaken process-identity checks merely to make scans faster.
- Do not hide the defect by only raising freshness thresholds or reducing the
  menu-bar refresh rate.
- Do not build a general-purpose local event bus unless the bounded projection
  cannot be implemented with existing watcher, state-file, and control seams.

## Invariants

### One local projection

The Machine Agent owns the merged local truth projection. The menu bar, native
device command, Python compatibility CLI, heartbeat payload, and doctor read it
or a serialized form of it. Provider scanners may produce evidence, but they do
not each own a competing session classification.

### Observation is not authority to terminate

Missing TUI attachment, missing wrapper, dead bridge, failed health probe, or
stale state file is evidence. It is never permission to kill a provider bridge,
app server, server process, or provider TUI. Existing explicit stop/terminate
contracts remain the only destructive paths.

### Provider mechanics remain distinct

The merged projection normalizes output shape, not evidence collection:

- Codex bridge lock, app-server pid, relay URL, and TUI attachment remain
  separate facts.
- Claude process identity and native channel readiness remain separate facts.
- OpenCode server health, attach presence, and lifecycle ownership remain
  separate facts.
- Cursor launcher pid, socket presence, and ready flag remain separate facts.
- unmanaged process binding remains distinct from managed control ownership.

### Last-known truth remains visible

A reconciliation in progress does not clear current sessions or replace the
panel with a generic failure. The projection retains the last coherent value,
marks its evidence age/provenance, and exposes reconciliation state separately.

### Reconciliation repairs drift

Event-driven updates optimize latency. Periodic reconciliation remains the
correctness backstop for missed filesystem events, ungraceful process exits,
PID reuse, manual state-file changes, and wake/sleep discontinuities.

### Freshness is scoped

The system must not reduce all freshness to one timestamp. At minimum it keeps:

- engine pulse time;
- projection generation time and version;
- last successful full reconciliation time;
- per-session or per-evidence observation time;
- reconciliation state and start time.

## Target Architecture

```text
provider/control signals       bounded reconciliation task
             |                            |
             v                            v
      daemon event loop <---- coherent observation result
             |
             v
   retained loop-local projection
      /          |           \
     v           v            v
status writer  heartbeat   diagnostics
     |
     v
engine-status.json -> Longhouse.app / CLI
```

### Retained loop-local projection

Do not introduce a new parallel model hierarchy. The daemon already owns the
right lifetime and most required types:

- managed observation scan results;
- cached unmanaged bindings;
- `SessionSnapshotState` with session digest/sequence;
- current control-channel state;
- current shipping/outbox state.

Retain the latest complete managed observations beside the cached unmanaged
bindings in the daemon loop. Add only timestamps and reconciliation metadata
that existing types do not carry. Reuse the existing session digest/sequence as
the projection version rather than inventing a second counter.

A reconciliation task produces a complete candidate result. The event loop
accepts that result atomically, derives the existing heartbeat/resolved-session
payload, and replaces the retained projection. A failed or partial task leaves
the previous projection intact.

Caching parsed state files by modification time is optional. Measure it after
the process/probe work is fixed: small JSON parsing is not the demonstrated
bottleneck.

### Live and slow observation classes

Current candidates receive the bounded awake liveness pass. Historical records
move to slow reconciliation only after two consecutive full reconciliations
prove the provider-specific fully-dead predicate:

- Codex: bridge not alive, app server not alive, and no TUI attachment;
- Claude: Claude and channel bridge not alive, with the existing valid process
  scan and reaping grace semantics satisfied;
- OpenCode: recorded server process is not identity-valid/alive and no matching
  TUI attachment exists;
- Cursor: launcher is not alive and the control socket is absent/unusable.

A degraded/orphaned control path is not fully dead. In particular, a dead Codex
bridge with a live app server remains current orphan evidence. Slow-class
records are retained for diagnosis and rediscovered at startup, wake, and the
slow safety interval; they are not probed at live cadence.

### Shared process inventory

One process inventory is shared across provider adapters for a reconciliation
pass. Providers may still apply different identity rules. The implementation
must not run separate full `ps` commands for Codex, Claude, OpenCode, and
unmanaged binding collection during the same pass.

The initial awake inventory interval is five seconds, plus immediate runs after
managed launch/state/control signals. This sets the known Codex/OpenCode TUI
attachment staleness bound. Hot liveness checks may target known current pids,
but a slower full inventory still revalidates command identity, process start
time, TUI attachment, and PID reuse. If a cheap check cannot preserve the
existing identity contract, keep the full check for that provider.

### Status/pulse writer

Status publication must not wait for a discovery task to finish. It snapshots
the retained projection and writes atomically. The writer must not run provider
discovery, `ps`, `lsof`, health probes, title discovery, or fallback unmanaged
collection.

The writer publishes:

- a fresh engine pulse;
- the current projection version and generation time;
- last reconciliation time/state;
- the cached session/control/shipping projection;
- a small stage-latency summary.

A fresh pulse with old evidence means “engine alive; reconciling/last-known
facts,” not `engine_status_aging`. A missing pulse means the engine itself may
be stopped or wedged.

The initial implementation may continue rewriting the complete roughly 20 KB
status file. A separate pulse file is allowed only if measurement proves the
atomic full write itself materially expensive; avoid adding another contract
without evidence.

Title, lease, phase-ledger, and other DB-derived fields must be retained or
refreshed by projection producers before publication. Missing unmanaged
evidence is represented as empty/last-known with unknown provenance, never as
permission for the writer to invoke broad discovery.

## Evidence Update Model

### Startup

1. Load any safe retained status projection for immediate last-known display.
2. Mark reconciliation `startup`.
3. Discover provider state files and unmanaged candidates once.
4. Build one shared process inventory.
5. Run bounded provider-specific liveness checks.
6. Publish a coherent projection.
7. Mark reconciliation complete and record duration.

The retained status file is presentation cache, not authority. Startup
reconciliation replaces it as evidence arrives.

### Managed provider state changes

Provider writers that already signal the Machine Agent update the same retained
projection through the daemon's existing control/wake seams. Do not require
every provider CLI to adopt a new protocol in the first implementation.

The five-second shared observation pass is the baseline for state changes that
have no signal, including Codex TUI attachment. Managed state-directory watchers
are conditional: add them only if measurement shows the bounded pass misses the
250 ms target for signaled changes or produces unacceptable state-file latency.
If added, coalesce by path and keep reconciliation as the correctness backstop.

### Process liveness and attachment

Process exit and TUI attach/detach can occur without a state-file write.
Therefore:

- current candidate pids receive cheap periodic checks;
- one shared process inventory runs at a measured bounded cadence;
- full identity reconciliation runs on startup, wake, relevant state change,
  and the slow safety interval;
- any uncertainty is represented as unknown/stale evidence rather than a false
  terminal claim.

### OpenCode health

Do not probe every historical OpenCode state file. Probe only records whose
server pid and identity are still viable. Bound concurrency and total probe
time. Cache the last result with observation time, and reconcile on relevant
state-file/process changes.

A failed probe changes OpenCode server-health evidence. It does not delete the
record or infer a different control owner.

### Unmanaged bindings

The unmanaged path should become process/hook/event-first:

1. Read and validate hook-observed bindings.
2. Start from the already-known current provider processes.
3. Use transcript paths already observed by the live file watcher or local
   binding store.
4. Run `lsof` only for unresolved unmanaged candidates.
5. Reserve broad provider-root discovery for startup, wake, or slow
   reconciliation.

If there are no unresolved unmanaged provider processes, skip transcript
discovery and `lsof` entirely.

Managed pids known to the current projection must not be reclassified as
unmanaged simply because their executable is a stock provider CLI.

### Wake recovery

Wake is a normal transition:

1. keep rendering the last coherent projection;
2. publish a fresh engine pulse;
3. mark reconciliation `wake` with `started_at`;
4. refresh current pid and control-channel evidence first;
5. reconcile historical state afterward;
6. clear the wake state only after a coherent projection is published.

The UI already has restrained wake copy. The producer must provide explicit
facts so the UI does not infer wake from a delayed file alone.

### Wake detection

The engine does not currently receive an OS wake notification. The first
implementation detects suspend/resume inside the daemon by comparing elapsed
wall-clock time with elapsed monotonic time on timer ticks. A material wall-time
jump beyond the monotonic delta marks a wake gap and starts one coalesced wake
reconciliation.

The threshold must comfortably exceed normal timer jitter and be covered by
deterministic tests with injected clocks. Record the detected gap and reason in
reconciliation telemetry. If a supported platform's monotonic clock includes
suspend time and cannot expose the gap, fall back to a platform wake source for
that platform rather than silently pretending wake detection works.

Wake detection owns reconciliation scheduling only. It does not infer that a
provider session ended or authorize process cleanup.

### Claude reaping and terminal signals

Claude dead-state-file reaping runs only from a successful full/slow
reconciliation with a valid process inventory. Preserve the existing grace
period and identity checks unless a separate product decision changes them.
Reducing scan cadence must not silently make reaping more aggressive.

`reconcile_claude_terminal_signals` runs whenever fresh Claude observations are
accepted into the retained projection. Status publication itself has no side
effects on Claude state or terminal signals.

## Freshness Contract

The status payload should add a backward-compatible block similar to:

```json
{
  "local_projection": {
    "version": 1842,
    "generated_at": "2026-07-16T15:42:43.548480Z",
    "engine_pulse_at": "2026-07-16T15:42:44.000000Z",
    "last_reconciled_at": "2026-07-16T15:42:40.100000Z",
    "reconciliation": {
      "state": "idle",
      "reason": null,
      "started_at": null
    }
  }
}
```

Compatibility rules:

- old readers continue using file age during rollout;
- new readers prefer `engine_pulse_at` for engine liveness;
- evidence freshness uses projection/per-row observation timestamps;
- a live pulse plus active reconciliation is not an aging warning;
- a live pulse plus reconciliation beyond its explicit budget becomes an
  inspectable reconciliation warning, not a stopped-engine repair;
- a missing/stale pulse retains the existing stopped/unavailable path;
- future timestamps remain clamped defensively as today.

Thresholds must follow measured producer contracts. Do not tune them until the
producer stages are separately observable.

`LocalStatusMonitor.semanticFingerprint` intentionally excludes pulse and
generated-at churn so one-second publication does not rerender the panel. It
includes reconciliation state/reason transitions and semantic session/control
changes so startup/wake/failure presentation still updates promptly.

## Failure Semantics

| Failure | Projection behavior | User implication |
| --- | --- | --- |
| one state file cannot parse | retain prior row, record scoped parse error | inspect affected provider/session |
| one provider probe times out | retain last evidence with age, mark provider row uncertain | scoped inspect, not global red |
| reconciliation task fails | retain complete prior projection, expose failure/retry | last-known status remains visible |
| status serialization/write fails | record/log write failure; next writer retries | pulse may age; repair only if persistent |
| process inventory unavailable | do not declare sessions dead | evidence unknown until next proof |
| engine process stopped | pulse becomes stale | existing repair-now behavior |
| wake during reconciliation | cancel/coalesce stale work and start wake reconciliation | calm updating state |

Partial scan results must not replace a complete projection. Apply evidence
updates per path/session only when they preserve a coherent row, or publish the
last complete snapshot until the pass is coherent.

## Observability Contract

Keep detailed stage timings in logs or the existing flight recorder:

- managed observation duration by provider;
- shared process inventory duration;
- OpenCode probe count and total duration;
- unmanaged discovery and `lsof` duration/call count;
- projection/enrichment build duration;
- serialization and atomic-write duration;
- blocking-task runtime and completion-to-event-loop-join delay;
- wake/startup reconciliation duration;
- status publication interval and signaled-update-to-publication latency.

Expose only a compact recent summary in `engine-status.json`: managed scan by
provider, process inventory, probe count/duration, publication interval, and
reconciliation duration/state. Add more only when a real diagnosis needs it.

Slow warnings must name the stage and provider. “Managed observation scan was
slow” is insufficient once the stages are measurable.

## Locked Implementation Decisions

The architecture review resolved the decisions that would otherwise block
implementation:

- Wake detection starts with wall-clock versus monotonic-clock gap detection;
  platform wake APIs are fallback only where that proof is unavailable.
- Claude state-file reaping belongs to successful slow/full reconciliation and
  retains the current grace and process-validity contract.
- Fully-dead relegation requires two successful full reconciliations and the
  provider-specific predicates in this spec.
- Shared awake process inventory starts at a five-second cadence; existing
  launch/state/control signals may trigger it sooner.
- Managed state-directory watchers are measurement-gated, not required
  architecture.
- The daemon's existing loop-local observations and `SessionSnapshotState` are
  the projection substrate; do not begin by building a new cache/model layer.

## Implementation Plan

### Phase 0: Measurement and regression fixture

- Add stage timers around the existing managed scan, unmanaged refresh,
  projection build, serialization, write, and event-loop join.
- Add a deterministic fixture corpus representing the dogfood shape: dozens of
  historical Codex/OpenCode state files, five live sessions, dead pids, one
  current OpenCode server, and mixed TUI attachment.
- Add a benchmark/QA command that reports scan count, duration percentiles,
  status cadence, and CPU sample instructions without touching real provider
  state.
- Record a pre-change dogfood baseline.

Deliverable: the current cost is attributable and repeatable.

### Phase 1: Decouple publication from reconciliation

- Retain the latest complete managed observations in daemon loop state beside
  the existing cached unmanaged bindings.
- Make one-second status publication snapshot retained state without awaiting
  a scan task.
- Add projection version, pulse, reconciliation, and evidence timestamps.
- Preserve last coherent projection through slow/failing reconciliation.
- Move title, lease, ledger, and other enrichment out of the publication path;
  remove the writer's inline unmanaged-discovery fallback.
- Make the five-minute Runtime Host heartbeat consume retained projection state
  instead of calling provider collectors synchronously.
- Update native and Python fast classifiers to distinguish pulse liveness from
  evidence/reconciliation age.
- Add compatibility tests for status payloads that predate the new block.

Deliverable: status/pulse cadence remains healthy even when a deliberately
injected reconciliation blocks.

### Phase 2: Share and bound managed observation work

- Build one process inventory per reconciliation and inject it into all
  provider adapters while preserving provider-specific identity rules.
- Restrict OpenCode health probes to viable current candidates and bound total
  probe time.
- Run the normal awake reconciliation every five seconds plus immediate passes
  for existing launch/state/control signals.
- Move fully-dead historical records to the slow class only through the locked
  predicates above.
- Move Claude reaping to successful slow/full reconciliation and keep terminal
  signals tied to accepted Claude evidence.
- Add clock-delta wake detection and coalesced wake reconciliation.

Deliverable: managed observation no longer stalls the event loop, attachment
truth has a five-second maximum unsignaled lag, and CPU falls without semantic
drift.

### Phase 3: Make unmanaged reconciliation incremental

- Validate hook-observed bindings before broad discovery.
- Start from current unresolved provider processes.
- Reuse paths observed by the live watcher/binding store.
- Avoid `lsof` for managed or already-resolved processes.
- Move full provider-root discovery to startup/wake/slow reconciliation.

Deliverable: idle unmanaged refresh does negligible work and cannot stall local
status publication.

### Phase 4: Add managed directory watchers only if measured

- Compare the Phase 2 five-second pass plus existing direct signals against the
  250 ms signaled-update target.
- If the target is missed because state-file changes have no signal, add
  bounded/coalesced managed state-directory watchers and update only affected
  evidence.
- Skip this phase when measured behavior already meets the product contract.

Deliverable: either exact evidence that watchers are unnecessary or the
smallest watcher implementation needed to close the measured gap.

### Phase 5: Native cold-start path

- Render initial local status from the native status-file reader or direct
  Swift decoding rather than waiting for the Python compatibility stack.
- Hydrate slower provider versions, update information, and deep diagnostics
  asynchronously.
- Keep `longhouse local-health --deep` as the explicit diagnostic path.
- Preserve the existing shared Swift presentation reducer and fixture suite.

Deliverable: cold/manual local status reaches the panel inside the 50 ms local
render budget on a warm machine.

### Phase 6: Dogfood, wake, and power proof

- Run the fixture and full menu-bar harness.
- Inspect all fixture PNGs if freshness/reconciliation copy changes.
- Run window and real menu-bar live modes.
- Exercise sleep/wake, network loss, provider attach/detach, active turn,
  explicit stop, and stale historical state.
- Compare pre/post engine CPU, status cadence, projection latency, and scan
  counts over a representative dogfood window.
- Run the ambient installer smoke if packaging or launchd wiring changes.

Deliverable: measured improvement on the installed dogfood app with no control
or health semantic regression.

## Acceptance Gate

### Correctness

- Header, badge, and panel still derive from the existing presentation reducer.
- Five current managed sessions remain five current managed sessions when the
  fixture also contains at least 60 historical state files.
- A missing process inventory never declares a session dead.
- A slow/failing reconciliation never clears the last coherent projection.
- Codex bridge/app-server processes are never terminated by observation code.
- Managed and unmanaged ownership do not cross-classify.
- The five-minute heartbeat path performs no synchronous provider discovery.
- Claude reaping retains its valid-process-scan requirement and grace semantics
  and runs only from successful full/slow reconciliation.
- Provider-specific attached/detached/degraded/orphan semantics remain covered
  by tests.
- Old status payloads continue to decode in the native and Swift readers.

### Performance

- Awake idle Machine Agent sustained CPU: below 2% on the dogfood Mac over a
  ten-minute observation window with no archive backlog.
- Local evidence signal to status-file publication: p95 below 250 ms, excluding
  the provider's own delay before emitting evidence.
- Status publication interval while awake: p99 below two seconds.
- Cached projection reduction plus serialization plus atomic write: p95 below
  20 ms for 100 current/residual records.
- No broad managed provider scan runs at one-second cadence.
- Unsignaled Codex/OpenCode TUI attach/detach appears within five seconds while
  the machine is awake.
- No unmanaged transcript-root discovery or `lsof` pass runs when there are no
  unresolved unmanaged candidates.
- Normal wake reconciliation: correct current projection within one second;
  if a provider probe exceeds that budget, last-known rows remain visible and
  reconciliation is explicit.
- Native cold snapshot decode/render preparation: p95 below 50 ms on warm
  dogfood.

### Operability

- Local health can distinguish stopped engine, live engine with stale evidence,
  reconciliation in progress, and reconciliation failure.
- Slow logs name the exact stage/provider and include elapsed time plus relevant
  record count.
- The benchmark artifact records build identity, fixture shape, and percentile
  results so comparisons are exact-SHA evidence.

## Test Strategy

### Rust unit/integration tests

- last coherent observations remain publishable while reconciliation blocks;
- status publication and Runtime Host heartbeat never invoke provider
  collectors or unmanaged discovery inline;
- shared process inventory feeds all provider adapters;
- stale/dead historical OpenCode rows are not health-probed at live cadence;
- fully-dead relegation requires two successful full reconciliations and the
  provider-specific predicate;
- Codex orphan evidence never enters the fully-dead class;
- Claude reaping runs only after valid full reconciliation and preserves grace;
- accepted Claude evidence still drives terminal-signal reconciliation;
- failed process inventory preserves prior liveness;
- blocked reconciliation does not block status publication;
- pulse, projection, and reconciliation timestamps advance independently;
- injected clock deltas coalesce one wake reconciliation;
- partial/error evidence cannot replace a complete row;
- unresolved unmanaged process gates the `lsof` fallback.

### Python compatibility tests

- fast local health reads the new projection block;
- pre-projection status payloads retain current behavior;
- pulse aging and evidence aging classify differently;
- deep diagnostics remain available and do not leak into the menu-bar hot path.

### Swift tests and fixtures

- live pulse plus wake reconciliation renders calm updating state;
- live pulse plus old provider evidence keeps scoped last-known session rows;
- stale pulse retains stopped/unavailable repair behavior;
- cached session titles survive status refresh;
- no presentation precedence changes for durability, needs-user, inspect,
  unavailable, and normal states.

### Live proof

- exact build identity recorded;
- archive backlog zero before CPU measurement;
- active managed session count and provider mix recorded;
- sleep/wake performed once with the app closed and once with the panel open;
- attach/detach at least one Codex or OpenCode TUI;
- run one unmanaged provider session to prove Shadow liveness still appears;
- inspect full-frame menu-bar fixture and live PNGs if copy changed.

## Rollout and Compatibility

1. Add fields; do not remove existing status fields.
2. Ship the engine producer first while readers continue using file age.
3. Ship native/Python reader support for pulse/projection freshness.
4. Ship Swift reader/presentation support if new user-visible distinctions are
   needed.
5. Dogfood through at least one wake cycle and one active multi-provider period.
6. Remove legacy scan scheduling only after projection parity and counters prove
   no provider disappears.

During rollout, a new engine with an old app must remain usable, and a new app
with an old engine must fall back to the existing file-age behavior.

## Risks and Countermeasures

### Missed events create stale truth

Countermeasure: startup/wake/slow reconciliation remains mandatory; every
projection row carries observation age; directory watches are an optimization,
not sole correctness authority.

### Cached process liveness accepts PID reuse

Countermeasure: preserve provider identity/start-time validation and perform
bounded full process reconciliation. Unknown is preferable to a false live
claim.

### Projection becomes another source of truth

Countermeasure: projection is reconstructable, in-memory, and derived from
existing provider/control/shipping evidence. The provider state plus local
engine stores remain durable inputs.

### More timestamps confuse the UI

Countermeasure: timestamps remain evidence fields. The existing presentation
reducer maps them to the small established freshness vocabulary: fresh,
updating after wake, stale, or unknown.

### Optimization accidentally reaps provider state

Countermeasure: keep slow-class relegation separate from explicit provider
cleanup. Relegation may reduce observation cadence; it may not terminate a
process or delete provider control state. Claude's existing dead state-file
cleanup remains an explicit slow-reconciliation responsibility with its own
validity and grace tests.

### Performance tests become flaky

Countermeasure: keep deterministic semantic tests in CI; run percentile/CPU
budgets through the dedicated benchmark and exact-build dogfood proof. Gate CI
only on stable bounded microbenchmarks.

## Explicitly Rejected Shortcuts

- Increase `ENGINE_FRESH_SECONDS` and call the warning fixed.
- Run the same full scan less often while leaving status publication coupled to
  it.
- Delete all historical bridge files automatically.
- Let the macOS app perform its own provider/process discovery.
- Treat filesystem events as perfectly reliable and remove reconciliation.
- Collapse provider evidence into a generic alive boolean.
- Add a daemon or database solely to cache this projection.
- Rewrite the menu-bar reducer before fixing the producer.

## Completion Decision

This plan is complete when local status behaves as a cheap, versioned projection
of current evidence; broad discovery is a bounded reconciliation mechanism; and
dogfood proves that the engine is quiet, session/control truth remains honest,
and wake no longer produces false engine-aging warnings.
