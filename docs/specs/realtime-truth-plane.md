# Realtime Truth Plane Epic

Status: Proposed for phased build
Owner: launch session surfaces
Last updated: 2026-05-05
Related:
- `session-signal-tier-model.md`
- `session-runtime-display-contract.md`
- `session-liveness-honesty.md`
- `local-agent-health-plane.md`
- `macos-menubar-control-surface.md`

## Goal

Longhouse should be trustworthy enough that a user checks the website, iOS app,
or macOS menu bar instead of inspecting raw terminal windows.

That requires two explicit contracts:

1. **Truth contract**: every surfaced state says what Longhouse actually knows,
   and does not infer a stronger claim from weaker evidence.
2. **Latency contract**: user-visible session/process/control-path state moves
   through a fast lane. Slow heartbeat and deep diagnostics are only repair and
   reconciliation paths.

## Product Standard

The user-facing surfaces must answer these questions quickly and honestly:

- Is this session Longhouse-managed or unmanaged?
- Is the underlying provider process running, closed, or not currently
  verifiable?
- Is there a managed control path right now?
- Is the session working, ready, blocked, idle, inactive, or closed?
- Is the machine online enough to trust current process/control observations?

These are separate axes. Do not collapse them into one colored pill.

## Fast vs Slow Lanes

### Must Be Fast

These states affect whether the user trusts an active card and must arrive in
the UI within two seconds on a healthy local network:

- provider phase signals: thinking, running tool, blocked, needs user, idle
- explicit provider terminal signals
- managed control-path attach/detach/degraded observations
- unmanaged provider process observed for a bound session
- unmanaged provider process gone for a previously bound session
- local runtime app/engine lifecycle that changes menu bar truth
- event publication needed to wake web and iOS subscribers after those writes

The fast lane is evented through hooks, engine outbox, runtime event writes, and
targeted snapshots. It must not wait for the five-minute machine heartbeat.

### May Be Slow

These facts are health, diagnostics, or reconciliation and may ride heartbeat or
manual deep checks:

- machine build identity and version
- disk free, parse errors, dead letters, spool backlog
- broad process inventory
- launchd/systemd service diagnostics
- full repair verification
- historical transcript catchup
- importer fallback scans

Slow facts can influence warnings and doctor output. They must not block menu
opening or active card status.

## Source-of-Truth Rules

- **Managed/unmanaged is truth, not capability copy.** Managed means Longhouse
  owns the control path for that session. It does not imply a currently
  steerable process unless the control-path axis says so.
- **Process running/closed is a process axis.** For sessions with a durable
  process binding, "closed" requires an explicit process-gone observation or a
  terminal provider signal. Missing heartbeat is "not verifiable," not closed.
- **Machine online is not session lifecycle.** A host heartbeat says the machine
  is reachable. It does not prove any particular provider process exists.
- **Transcript progress is activity, not runtime phase.** It may update recency
  and content. It must not invent "running" or "closed."
- **Deep local-health is not a UI dependency.** Whole-machine process scans are
  diagnostics unless their result has already been distilled into a fast
  session/process snapshot.

## Latency SLAs

These are engineering budgets, not loose aspirations:

- Web/iOS phase update after hook/outbox write: p95 under 2s.
- Web/iOS bound process observed/gone update: p95 under 2s after local
  observation.
- Managed control-path attach/detach/degraded update: p95 under 2s after local
  observation.
- Menu bar open from last-good cache: under 100ms before any refresh completes.
- `longhouse local-health --fast --json`: p95 under 500ms on a laptop with
  10k+ processes.
- Menu bar refresh subprocesses: single-flight; no overlapping local-health
  probes.
- `longhouse local-health --deep --json`: may take longer, but must never be
  used by default menu open/refresh.
- Five-minute heartbeat: allowed only for machine health, build identity,
  repair, and reconciliation.

## Epic Phases

### Phase 0 - Contract and Review

Deliverables:

- This epic spec committed.
- Hatch Opus review of the phase plan before implementation.

Success criteria:

- The plan names each user-visible state axis.
- Each signal class is classified fast or slow.
- Success criteria include real web, hosted, local engine, and menu bar
  validation.

### Phase 1 - Fast Publication and Provenance

Problem:

The runtime already has fast event writes, but some writes are not clearly
published to subscribers and provider provenance is lossy.

Deliverables:

- Presence/runtime writes preserve provider-specific source provenance, for
  example `codex_hook` vs `claude_hook`.
- Any fast runtime write that changes visible session state wakes timeline and
  session subscribers.
- Backend tests cover provider-specific provenance and subscriber publication.

Success criteria:

- A Codex presence event is stored/projected as Codex-originated, not
  Claude-originated.
- A fast presence/runtime write causes web/iOS subscriber payloads to refresh
  without waiting for heartbeat.
- Targeted backend tests pass.
- Hatch Opus review accepts the implementation before the next large phase.

### Phase 2 - Fast Local-Health and Menu Bar Startup

Problem:

The macOS menu bar can open on a cold in-memory state and immediately block on a
deep `local-health` subprocess. Deep local-health currently performs a broad
macOS process scan and can take tens of seconds on a busy laptop.

Deliverables:

- Split CLI contract into `longhouse local-health --fast --json` and
  `longhouse local-health --deep --json`; keep existing default compatible but
  move menu bar to the fast tier.
- Fast tier reads cheap state only: engine status, outbox counts, cached
  session/process/control snapshots, service headline, build identity.
- Deep tier owns whole-machine process scans and repair diagnostics.
- Menu bar persists a last-good snapshot and renders it synchronously at app
  startup/open.
- Menu bar refresh is single-flight and cannot pile up local-health
  subprocesses.
- Fixture and live menu bar harness runs cover startup, refreshing, degraded
  machine, managed attached/detached/degraded, unmanaged process running, and
  closed states.

Success criteria:

- Menu bar opens from cache under 100ms.
- Fast local-health stays under 500ms on David's laptop.
- Deep local-health remains available for Doctor/repair.
- No menu bar default path runs broad process scans.
- `make menubar-harness-full` passes and PNGs are visually inspected.
- Hatch Opus review accepts the implementation before the next large phase.

### Phase 3 - Fast Process and Control-Path Snapshots

Problem:

Unmanaged process bindings and managed control-path leases are still largely
heartbeat-shaped. That makes active cards stale for up to five minutes after a
terminal closes, a managed bridge detaches, or a bare Codex/Claude process is
observed.

Deliverables:

- Rust engine emits targeted session process/control observations through the
  fast runtime path, not only the five-minute heartbeat.
- Runtime Host stores durable process binding facts with observed/gone
  timestamps and source identity.
- Managed control-path state uses explicit attached, detached, degraded, and
  orphan-bridge observations.
- Heartbeat keeps reconciling missed facts but is no longer the primary user
  visible path for these states.

Success criteria:

- Closing a bound Codex/Claude terminal updates the hosted card within 2s of the
  local observation.
- Opening or discovering a bound unmanaged provider process updates the hosted
  card within 2s.
- Managed bridge detach/degraded/orphan states update within 2s.
- Synthetic integration tests cover observed, gone, stale-host, and reappeared
  process bindings.
- Hatch Opus review accepts the implementation before the next large phase.

### Phase 4 - Lifecycle Semantics Across Clients

Problem:

Web, iOS, and menu bar have historically re-derived state from raw fields and
overloaded colors/pills. The server must own the display meaning; clients should
render it.

Deliverables:

- `runtime_display` and `timeline_card` expose the axes clients need:
  control path, signal tier, lifecycle, host state, process state, control-path
  state, display label, tone, and timestamp prefix.
- Web cards and session detail use the server projection for these axes.
- iOS timeline/session/widget models use the same projection with optional
  backward-compatible fields.
- Menu bar uses the fast local-health snapshot with the same vocabulary.
- Remove client heuristics that infer closed/running from `ended_at`,
  heartbeat freshness, or branch/provider metadata.

Success criteria:

- The same fixture session renders the same semantic label/tone on web, iOS,
  and menu bar.
- "Closed" appears only from explicit terminal truth or process-gone truth.
- "Managed" never implies steerable unless the control-path axis says attached.
- "Host online" is not shown as a primary session status.
- Cross-client fixture tests or snapshots cover the matrix.
- Hatch Opus review accepts the implementation.

### Phase 5 - Real-World Ship and Dogfood Validation

Deliverables:

- `make test-ci` or an agreed targeted equivalent passes before push.
- Runtime ship to hosted surfaces using exact SHA.
- Hosted david010 reprovision/verification completes.
- `make qa-live` passes against david010 or the relevant hosted target.
- `make dogfood-refresh` runs on David's laptop.
- `launchctl kickstart -k gui/$(id -u)/ai.longhouse.app` restarts the menu bar.
- Live QA checks actual cards on david010 for:
  - active unmanaged Claude
  - active unmanaged Codex
  - closed unmanaged Codex/Claude
  - managed sessions with no open managed process on the laptop
  - stale or unknown process truth
  - menu bar installed-app state

Success criteria:

- Hosted web, local CLI/engine, and macOS menu bar all report the same semantic
  truth for David's current sessions.
- Process closed/observed propagation is no longer heartbeat-bound.
- Any remaining unknown state is labeled as unverifiable, not collapsed to
  closed or ready.
- Final report names exact live SHA, validation commands, and any residual
  product gaps.

## Integration Test Matrix

The implementation must preserve or add coverage for:

| Scenario | Expected label | Fast? |
| --- | --- | --- |
| Managed bridge attached and thinking | Working / Thinking | yes |
| Managed bridge attached and idle/needs user | Ready | yes |
| Managed bridge detached, provider may still exist | Control offline / detached | yes |
| Managed bridge orphaned without session control | Orphan bridge attention | yes |
| Unmanaged process observed | Process running / Active | yes |
| Previously observed unmanaged process gone | Closed / Process ended | yes |
| Host stale before process-gone proof | Cannot verify | slow/reconcile |
| Transcript-only recent activity | Recent activity | mixed |
| Transcript-only stale activity | Inactive / Unknown | mixed |
| Machine health degraded, sessions still visible | Machine degraded + per-session states | slow |
| Menu bar cold app restart | Last-good snapshot + refreshing affordance | fast local |
| Deep doctor run on 10k processes | Diagnostic output, not UI blocking | slow |

## Non-Goals

- Do not make unmanaged sessions steerable by implication.
- Do not replace heartbeat; demote it to reconciliation where appropriate.
- Do not make the menu bar parse launchd/process internals directly.
- Do not ship an iOS release path; if iOS code changes, David still needs a
  manual Xcode build.
