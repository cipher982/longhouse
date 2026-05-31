# Managed Session Propagation Profiler

## Purpose

Longhouse must feel like a faithful extension of the user's raw terminal, not a delayed or lossy dashboard.

If a user starts Claude or Codex normally, Longhouse should show the same essential truth the terminal would show: the session exists, it is doing work or waiting, its transcript is current, and it is closed when the process is actually gone. If a user starts through Longhouse, the managed control path should improve that experience without hiding provider behavior or inventing state.

This profiler is the long-lived experiment suite for that trust contract.

## Core Question

How closely does Longhouse replicate the user experience of not using Longhouse?

The profiler answers that by comparing five views of the same session:

- **Raw provider truth**: local provider process and provider transcript.
- **Machine truth**: local Longhouse health, hook outbox, bridge/channel state, and engine heartbeat.
- **Reducer-input truth**: runtime ingest events, phase ledger rows, process-scan snapshots, and managed-session lease snapshots that feed `session_runtime_state`.
- **Runtime truth**: hosted or self-hosted Runtime Host database/API state, especially `session_runtime_state`.
- **Client truth**: timeline REST API, timeline SSE, browser card presentation, and later iOS timeline presentation.

The result should say where truth changed first, where it arrived late, and whether any surface lied.

The profiler is no longer one experiment. It is a small profiling system with
separate profiles for cold truth, warm realtime truth, durable archive truth,
honest degradation, and fidelity. Those profiles share one observation schema
and one report vocabulary so a run can point at the guilty layer instead of
producing a vague "timeline slow" failure.

## Goals

- Measure propagation from local terminal events to Runtime Host state and timeline cards.
- Separate provider latency from Longhouse telemetry latency.
- Verify managed and unmanaged session lifecycle truth for Codex and Claude.
- Establish a regression harness that can start local and later move into CI or nightly dogfood monitoring.
- Produce per-run artifacts that are useful for debugging without reading full transcripts.
- Make user-visible trust failures explicit: stale running labels, missing cards, delayed close, wrong managed/unmanaged ownership, and mismatched active/idle/closed state.
- Give a solo developer a repeatable substitute for a frontend/QA team: browser-visible assertions, stack-decomposed timing, and p95 trend data.

## Non-Goals

- Do not benchmark model quality or provider response speed as a Longhouse metric.
- Do not require SSH, mobile, or network-loss scenarios in the first version.
- Do not collapse Codex and Claude into one implementation path. The providers have different managed mechanics.
- Do not use hidden fallbacks to make the profiler pass.

## Profile Classes

Use named profile classes instead of one blended "propagation" number.

### Cold Timeline Truth

Question: if a user opens Longhouse from scratch, how long until the page shows
the correct session truth?

This profile intentionally includes browser launch, navigation, auth cookie
acceptance, JavaScript boot, initial timeline query, React render, and browser
paint. It catches broken deploys, slow initial timeline listings, auth/session
cookie regressions, app boot errors, and card rendering failures. It must not
be used as the realtime propagation SLA.

Primary metrics:

- `cold_timeline_loaded_ms`
- `cold_timeline_card_paint_ms`
- `cold_timeline_closed_card_paint_ms`
- page console errors and failed network requests

### Warm Realtime Timeline

Question: if the user is already on `/timeline` with the stream connected, how
long until a real change is visible?

This is the core product trust profile. The browser should be warm before the
session change is triggered. The profile must wait for initial data and timeline
SSE connection, then trigger the action under test.

Warm realtime is split into three sub-profiles because session create, live
output, and close use different product contracts and can regress separately.

#### Warm Session Create

Question: after the browser is warm, how long until a newly launched managed
session is visible as a timeline card with correct ownership and capability
state?

Primary metrics:

- `warm_session_created_to_sse_ms`
- `warm_session_created_to_card_paint_ms`

#### Warm Live Output

Question: after the browser is warm and a managed session is open, how long
until local provider progress or generated text is visible on the card?

Primary metrics:

- `warm_live_output_local_to_sse_ms`
- `warm_live_output_local_to_paint_ms`
- `warm_live_output_sse_to_paint_ms`

#### Warm Close

Question: after the browser is warm and a managed session exits gracefully, how
long until the timeline shows the session as closed?

Primary metrics:

- `warm_close_local_to_paint_ms`
- `warm_close_local_to_db_ms`

Diagnostic layer metrics:

- `warm_close_local_to_sse_ms`
- `warm_close_sse_to_paint_ms`

The close-SSE watcher metrics are diagnostic only. They measure the profiler's
extra observer, not the already-open page's own stream-to-paint path, so they
must not be used as the user-visible close SLA.

Target posture after the primary paths are stable: warm live and warm close
should be p95 < 500ms on nominal network for managed sessions, with a hard
product alarm at p95 > 1000ms.

#### Warm Preconditions

A warm realtime profile may trigger the session action only after all of these
are true:

- the browser page has loaded `/timeline`
- the initial `/api/timeline/sessions` request has returned successfully
- the timeline SSE connection is established and has delivered its first event,
  heartbeat, or explicit ready marker after connection open
- the observer has recorded `warm_ready_at`

Without those preconditions, the run is a cold timeline profile or a browser
setup failure, not a warm realtime propagation measurement.

If the existing timeline stream does not emit anything until data changes, add
an explicit ready marker before promoting warm profiles into SLA data.

### Durable Archive

Question: can Longhouse reconstruct what happened correctly after the fact?

This profile follows provider transcript append through canonical event rows,
searchable timeline history, and replay/export. It should not block the warm
live lane. Slow archive is a durability/catch-up issue, not a reason for the
card to wait before showing current truth.

Primary metrics:

- `provider_append_to_hosted_event_ms`
- `archive_event_count_delta`
- missing/duplicate user, assistant, and tool events
- non-text payload preservation

Target posture: p95 < 3s for the happy path, alert if a managed completed turn
is not durable after 30s.

### Honest Degradation

Question: when terminal, process, SSH, or network conditions fail, does the UI
say the most honest thing?

This profile starts after graceful close is already reliable in the warm close
profile. It distinguishes terminal disconnect, process gone, active-turn
detach, host stale/offline, and hosted ingest loss. It must not collapse "SSH
disappeared" into "the provider process is gone" without evidence.

Primary metrics:

- `terminal_detached_to_card_state_ms`
- `process_gone_to_card_state_ms`
- `machine_stale_to_card_state_ms`
- `hosted_reconnect_reconciliation_ms`
- terminal reason/source fidelity

### Fidelity Checks

Question: did Longhouse copy the right truth, not merely copy something fast?

Fidelity checks cut across the latency profiles. They may run as a standalone
`profile_class=fidelity` replay or as assertions inside another profile.

Primary checks:

- transcript event counts and payload preservation
- capability/action agreement
- lifecycle reason/source accuracy
- browser/iOS row pairing equivalence
- close/detached/offline label accuracy

## Stack Decomposition

Every profile should decompose the path into these layers when applicable:

```text
raw provider/process
  -> provider transcript / bridge/channel observation
  -> Machine Agent / hook outbox / bridge runtime event
  -> Runtime Host ingest and reducer
  -> hosted DB/API truth
  -> timeline SSE event
  -> browser receive/cache update
  -> React commit and browser paint
```

The report should prefer layer-specific names over user-facing shorthand. For
example, "close slow" should become "terminal event reached hosted DB after
5.1s; browser painted 243ms later."

## CI And Monitoring Ladder

The system should grow in layers:

1. **PR/local fast gate**
   - unit tests for runtime display/liveness reducers
   - profiler artifact parsing/report tests
   - fixture-backed browser card semantic assertions

2. **Pre-push/manual profiler**
   - one warm managed Codex close profile
   - one warm managed Codex live-output profile
   - provider-timeout classification profile

3. **Nightly dogfood**
   - repeated warm realtime profiles, p50/p95 over N runs
   - cold timeline truth profile
   - durable archive profile
   - close while idle, close while thinking, provider timeout
   - run on one named always-on dogfood host with cost ceilings and alerting
   - keep latency budgets warn-only until at least 20 clean baseline runs exist

4. **Release/ship confidence**
   - one hosted warm live-output run
   - one hosted warm close run
   - screenshot/DOM assertions that timeline cards expose the right semantic pills

5. **Soak and hostile cases**
   - SSH disconnect, terminal close, process kill, sleep/wake, and network loss
   - these are not per-PR gates; they are product truth monitors.

Single clean runs are evidence that a path exists. Gates must eventually be
p95-over-N, and provider/environment preconditions must be excluded from
propagation verdicts.

## Code Architecture Target

`scripts/ops/profile-managed-session-propagation.py` began as a focused harness
and has grown into a 3000+ line script. The goal is to make the profiler easy
to extend without pausing feature work for a broad rewrite.

Target shape:

```text
scripts/ops/profile-managed-session-propagation.py   # CLI entrypoint and current orchestration
scripts/ops/managed_profiler/
  browser_ui_observer.mjs                            # Playwright DOM observer
  browser.py                                         # browser process wrapper, after warm profiles exist
  hosted.py                                          # hosted DB/API/SSE probes, after duplication is real
  codex.py                                           # managed Codex driver, after warm profiles exist
  observations.py                                    # schema, recorder, deltas
  reports.py                                         # metrics + markdown summary
```

Refactor rules:

- Move one layer at a time and preserve artifact schema compatibility.
- Extract the browser observer script first; inline JavaScript is the current
  highest-friction part of the harness.
- Do not introduce a provider abstraction until Claude has a real profile.
- Keep generated per-run observer scripts as artifacts when they materially aid debugging.
- Do not change Longhouse product behavior while refactoring the profiler.
- Prefer boring subprocess boundaries for browser observers; the profiler should stay usable without a dev server.
- Keep source labels and metric names stable; bump `METRICS_SCHEMA_VERSION` when semantics change.

## Trust Contracts

These are the contracts the experiment should eventually enforce.

1. **Session creation is visible.**
   A real local provider session should appear in the timeline with the correct provider, project, machine, ownership, and capability state.

2. **Ownership and capabilities are truthful.**
   The profiler should measure `execution_home`, `managed_transport`, `capabilities.live_control_available`, and `capabilities.host_reattach_available` directly. Do not infer managed state only from the launch command. A managed session can be live-control available, host-reattachable, or observe-only depending on the actual control path.

3. **Working vs idle is provider/machine truth, not wishful UI state.**
   The website should not show a session as actively working unless there is a current provider, hook, bridge, or channel signal that supports it.

4. **Graceful process exit closes cleanly.**
   A smooth local shutdown should become `Closed` on the timeline. Managed shutdown should use the managed control path where available; unmanaged shutdown may rely on process observation.

5. **A stale terminal relationship is not the same thing as a gone process.**
   Later SSH and network experiments must distinguish:
   - provider process still alive, terminal detached
   - provider process gone
   - machine offline
   - Runtime Host unable to receive fresh machine truth

6. **Runtime state is the lifecycle source of truth.**
   Browser labels should follow the runtime view/reducer contract, not independent frontend inference.

7. **Detached is not closed.**
   Use `detached` for "provider process is alive but the original terminal/SSH relationship is gone." Use `closed` only when the provider/control process is gone or a terminal lifecycle event says the session ended.

## Lifecycle Propagation Contract

Lifecycle truth is eventful on the primary path and inferred only on backstop paths. A managed control path that knows it is closing must push a terminal lifecycle event immediately; the Runtime Host must not wait for the next heartbeat, process scan, lease expiry, or transcript import to discover that fact when the primary path is healthy.

The latency budgets below are target budgets until the profiler records enough runs to report p50/p95 honestly. A single sub-second run proves the primary path exists; it does not prove the SLA distribution. The profiler must report DB ingest, API/SSE, and rendered-card latency separately before the product can claim a user-visible SLA.

## Live UI vs Durable Archive

Managed sessions have two different truth lanes:

- **Managed live UI lane**: control-path runtime observations from bridge/channel state. This lane owns current UI truth: running, thinking, idle, closed, active tool, and latest live output. Target: p95 < 500ms from local bridge/channel observation to timeline API/SSE visibility on nominal networks.
- **Durable archive lane**: transcript file ingest into canonical `events` and turn durability. This lane owns history, search, replay, and reconciliation. Target: p95 < 5s from provider transcript append/turn completion to durable canonical events, with alerting when a managed runtime-completed turn is not durable after 30s.

The profiler must report both lanes. A managed card can be truthful in the live UI lane before the transcript archive has caught up. That is not a failure; it is the intended architecture. A live UI that waits for transcript ingest is a failure for managed sessions. Because the browser timeline consumes `/api/timeline/sessions/stream`, the managed live UI verdict should use timeline SSE first and keep REST polling as a comparison/backstop measurement.

The profiler must fail fast on provider/environment preconditions before measuring propagation. For example, if Codex says hooks need review, the run is `blocked`, not a live-UI or durable-archive regression. The harness should close the managed bridge and record the precondition instead of waiting for transcript timeouts. For Codex, prefer the structured app-server `hooks/list` trust status over scraping the TUI log; the TUI text is a fallback when the structured path is unavailable.

Hook approval is a provider trust boundary. The profiler may offer an explicit operator flag to trust only the Longhouse-installed Codex hooks by writing the corresponding `hooks.state` hashes through Codex's own `config/batchWrite` API, but it must not silently approve arbitrary hooks as a side effect of running a propagation test.

The transcript shipper can and should be improved for archive freshness, but it must not be the primary realtime state source for managed sessions. For managed Codex, the bridge is the clock and rollout JSONL ingest is the ledger.

| Lane | Point Of Truth | Terminal Signal | Target Budget | Backstop Budget | Notes |
| --- | --- | --- | --- | --- | --- |
| Managed Codex bridge | `longhouse-engine codex-bridge` daemon | `terminal_signal` from `codex_bridge` with `terminal_state=session_ended`, `terminal_reason=bridge_stop` | target p50 < 250ms, p95 < 1000ms after graceful stop is requested | heartbeat missing lease < 2min until replaced | The bridge already owns API credentials and posts runtime phase/progress observations, so graceful stop is a push event. Until runtime observations are spooled, this primary path is best-effort when hosted is unreachable. |
| Managed Claude channel | Claude native channel/control wrapper plus Machine Agent channel scan | `terminal_signal` from wrapper with `terminal_reason=provider_exit`; fallback `terminal_signal` from `claude_channel_scan` with `terminal_reason=process_gone` or `channel_state_gone` | target p50 < 250ms, p95 < 1000ms after provider exit is observed by the wrapper/channel | Machine Agent channel scan < 2s on a healthy awake machine | Claude must not borrow Codex bridge semantics; it needs its own channel/process lifecycle source. The wrapper is the graceful fast path; the scan is the process-truth backstop. |
| Hooked unmanaged provider | provider hook/wrapper when available | `terminal_signal` with `terminal_reason=provider_exit` and exit metadata | p50 < 1s, p95 < 5s after provider exit | complete engine process snapshot < 2min | This is still not managed control. It is a truthful terminal observation from a local hook/wrapper. |
| Scanned unmanaged provider | Machine Agent process snapshot | `terminal_signal` with `terminal_reason=process_gone` | no near-instant SLA | p95 < 2min after complete snapshot proves absence | This is a compatibility/backstop lane, not the trust-critical managed lane. |
| Machine offline / network loss | Machine heartbeat and Runtime Host freshness | stale/offline state, not terminal close | p95 < 10s for stale machine indication once freshness expires | explicit reconnect repair | Offline is not closed. A session can be disconnected from hosted while the provider process remains alive. |

The profiler should fail a managed graceful shutdown if the first terminal event is `engine_attached_lease` or another missing-lease inference. Those sources are acceptable only as backstops and should be labeled as such in artifacts.

### Phase Plan

1. **Managed Codex push-close.**
   Make `codex-bridge` emit an immediate terminal runtime event when the bridge receives a graceful IPC stop. Run the managed Codex profiler before and after; success means hosted runtime and timeline close from `source=codex_bridge` within the target budget.

2. **Terminal reason/source model.**
   Persist terminal reason/source separately from `terminal_state` so the UI and profiler can distinguish `bridge_stop`, `provider_exit`, `process_gone`, `host_expired`, and `heartbeat_gap` without parsing payload JSON.

3. **Managed Claude lifecycle event.**
   Add the equivalent channel/control-path terminal event for managed Claude. Its implementation should follow Claude's actual process/channel mechanics, not Codex bridge state. The current implementation has a wrapper graceful-exit signal and a Machine Agent `claude_channel_scan` process-gone backstop; the remaining promotion work is repeated profiling and durable archive reliability.

4. **Unmanaged close improvements.**
   Add a best-effort provider exit hook/outbox event where available, then tighten the scanned unmanaged backstop separately. The product should communicate that scanned unmanaged close is less immediate than managed close.

5. **Profiler hardening.**
   Keep SSE measurement aligned with the browser timeline stream, record terminal source/reason directly, add SLA verdicts per lane, and classify backstop closures as failures for managed graceful shutdown cases.

6. **Runtime event durability.**
   Decide whether runtime terminal events are part of the hard SLA or an opportunistic fast path. If hard SLA, route them through a retry/spool path before treating the managed close contract as met under flaky network conditions.

## Fidelity Metrics

Latency is not enough. The profiler must also measure whether Longhouse copied the right truth.

- **Transcript fidelity**: compare local provider artifacts against Runtime Host events. Count missing assistant messages, missing user prompts, missing tool results, duplicate rows, and dropped non-text payloads.
- **Capability fidelity**: attempt the advertised action. If `capabilities.live_control_available=true`, a send should work. If it is false, the UI should not invite live control.
- **Lifecycle fidelity**: compare actual shutdown cause with Runtime Host terminal state and card status. `process_gone`, graceful provider exit, wrapper exit, detached terminal, and machine offline are different facts.
- **Pairing fidelity**: later browser/iOS layers should render equivalent transcript row counts and tool-call pairings for the same session.
- **Close-cause accuracy**: card status should explain why a session closed or detached, not merely stop updating.

## Clocks

Propagation measurements cross process and machine boundaries, so clock handling must be explicit.

- Use the harness wall clock as the primary report clock.
- Record monotonic time only for ordering observations within the same process.
- Sample clock skew for local machine vs Runtime Host at run start and run end.
- Never subtract monotonic timestamps from different processes or machines.
- Report latency as wall-clock delta with the recorded skew context.

Clock skew sampling should be explicit in the artifact. Prefer a Runtime Host timestamp from the canonical API/health surface when available. If that is not exposed, record the HTTP `Date` header and a hosted database `CURRENT_TIMESTAMP` sample as lower-fidelity skew context. Mark sub-second latency claims as approximate unless skew is known.

## Environment Controls

Every run should record:

- harness version
- local git SHA and dirty state
- Runtime Host image/build SHA
- engine build identity
- CLI build identity
- Machine Agent status and version
- current machine name
- sleep/wake windows during the run, if any
- count of other live managed sessions on the same machine
- count of concurrent sessions in the target project
- hosted Runtime Host SHA changes during the run

If the hosted runtime SHA changes mid-run, mark the run as contaminated rather than treating failures as product evidence.

## Initial Experiment Ladder

Start with the cleanest possible local behavior. Only introduce hostile cases after the best path is boring.

### Phase 1: Direct Graceful Shutdown

| Case | Provider | Ownership | Launch | Shutdown | Expected User-Visible Result |
| --- | --- | --- | --- | --- | --- |
| A1 | Codex | Unmanaged | bare provider CLI | smooth provider quit / `Ctrl-C` | card appears, transcript syncs, then closes |
| A2 | Claude | Unmanaged | bare provider CLI | smooth provider quit / `Ctrl-C` | card appears, transcript syncs, then closes |
| B1 | Codex | Managed | `longhouse codex` | smooth provider quit / `Ctrl-C` | managed card appears, prompt/response syncs, bridge closes |
| B2 | Claude | Managed | `longhouse claude` | smooth provider quit / `Ctrl-C` | managed card appears, prompt/response syncs, channel/process closes |
| B3 | Codex/Claude | Managed observe-only | managed launch without live remote send capability | smooth provider quit | card says managed but does not advertise unavailable live control |

Provider-specific shutdown commands matter. For Codex, graceful managed close should include `longhouse-engine codex-bridge stop` and normal TUI exit. For Claude, `Ctrl-C` can mean interrupt rather than quit; graceful close should use Claude's actual exit path, such as `/exit` or the provider-supported equivalent, and record the exact method used.

### Phase 2: Local Hostile Shutdown

| Case | Shutdown Mode | Purpose |
| --- | --- | --- |
| C1 | close terminal window/tab | terminal relationship disappears, provider may or may not survive |
| C2 | kill wrapper process | verifies orphan detection and cleanup |
| C3 | send `TERM` to provider child | verifies graceful process-gone lifecycle |
| C4 | send `KILL` to provider child | verifies hard process-gone lifecycle |
| C5 | interrupt during active turn | verifies active-turn recovery and stale working state |

### Phase 3: SSH And Network Semantics

Only run after Phase 1 and Phase 2 have clear baselines.

| Case | Scenario | Product Question |
| --- | --- | --- |
| D1 | SSH session exits cleanly | does provider process close, and does Longhouse report it? |
| D2 | SSH client disconnects unexpectedly | does provider survive, and does the UI distinguish detached from closed? |
| D3 | machine loses network then returns | does hosted state show stale/offline instead of false live truth? |
| D4 | laptop sleeps/wakes | does awake-time vs wall-time behavior remain explicit? |

## Provider Mechanics

The profiler should expose a common measurement model, but it must not pretend providers work the same way.

### Codex

Managed Codex uses:

- `longhouse codex`
- `longhouse-engine codex-bridge`
- Codex `app-server`
- the Codex WebSocket relay in front of `app-server`
- bridge state under `~/.longhouse/managed-local/codex-bridge/`
- rollout JSONL under `~/.codex/sessions/...`
- TUI logs under `~/.codex/log/codex-tui.log`
- `longhouse-engine codex-bridge send`
- `longhouse-engine codex-bridge stop`

Codex is the easiest first target because managed sessions can be launched in detached-UI mode with `--no-attach`, then driven through the bridge.

The profiler should treat rollout JSONL as provider transcript truth. Bridge `last_turn_status` can be stale; a stale bridge field is a bridge-state finding, not proof the provider turn is still running. If an app-server child survives with no bridge `.sock` or `.json`, classify it as an orphan/cleanup failure.

### Claude

Managed Claude uses:

- `longhouse claude`
- Claude native channel/MCP/stdin control
- channel state under `~/.claude/channels/longhouse/sessions/`
- `longhouse claude-channel send`
- `longhouse claude-channel interrupt`
- process scan for liveness

Claude requires an actual channel-backed Claude process. A detached/no-attach launch prepares state, but does not by itself create the provider process to measure.

Claude liveness is noisier because it depends heavily on process scan plus channel state. The profiler should record the channel state file and process identity every time it classifies Claude as live, detached, or closed.

The first Claude slice is observation-only: `scripts/ops/probe-managed-claude-truth.py`
attaches to an existing managed Claude session id and records the truth surfaces
needed to design the real driver, including channel state, hook outbox phase
rows, provider process identity, channel health, and hosted runtime truth. It
should graduate into the shared profiler only after one live managed-Claude run
proves which local signal is the primary clock for create, turn phase, and
close.

Current hypothesis to validate: channel-state `ready` is the create/attach
clock, hook outbox phase rows are the turn-state clock, and provider/bridge PID
truth is the close/degraded clock. Hosted runtime and browser paint are
downstream propagation layers, not primary Claude truth.

Current POC driver: `scripts/ops/run-managed-claude-poc.py`, exposed as
`make managed-claude-poc`. It launches `longhouse claude` in a PTY, confirms
Claude's development-channel prompt, sends one channel message, waits for a
real assistant message in the local Claude transcript, exits with `/exit`, and
runs the managed-Claude truth probe around the lifecycle. The transcript gate
is intentional: matching terminal text is insufficient because Claude displays
the injected prompt text before any assistant response exists.

Initial clean finding from 2026-05-12: managed Claude channel send works and a
Haiku-backed assistant response was durable in the local transcript about 3.3s
after channel injection. Smooth local `/exit` removed the provider process in
about 600ms. Hosted runtime did not receive a terminal close event for that run
and stayed `idle`; that is a lifecycle propagation failure for managed Claude,
not a provider-response failure. A Sonnet run during the same session was
classified as provider-contaminated because Anthropic returned repeated 529
overload errors and no assistant message was produced.

Follow-up implementation from the same investigation added two managed-Claude
close lanes:

- `claude_channel_wrapper` posts graceful `session_ended/provider_exit` when the
  `longhouse claude` wrapper returns from the provider process.
- `claude_channel_scan` runs in the Machine Agent and posts
  `process_gone/process_gone` or `process_gone/channel_state_gone` when a
  previously observed managed Claude channel loses its provider PID or channel
  state file. This is the manual-attach and failed-wrapper-POST backstop.

These live-lifecycle lanes do not prove durable archive correctness. Every
managed-Claude POC report must continue to print both runtime terminal truth
and durable hosted archive event counts.

The same session also found a lower-layer transport issue: machine presence
POSTs without a user agent were blocked by Cloudflare (`403`/`1010`) while
`curl` succeeded. The engine now sends an explicit `longhouse-engine/<version>`
user agent and caps presence POSTs with a short timeout plus one in-flight
presence drain. These fixes keep local hooks from piling up behind blocked or
slow `/api/agents/presence` calls, but they do not solve hosted write-serializer
backpressure under heavy concurrent agent traffic. If hosted ingest or presence
is slow, classify the run as infrastructure/runtime contaminated before using
it for provider-specific SLA claims.

## Measurements

Every run should write a structured JSONL artifact. Each observation must include:

- `harness_version`
- `run_id`
- `profile_class`: `cold_timeline`, `warm_realtime`, `durable_archive`,
  `honest_degradation`, or `fidelity`
- `case_id`
- `provider`
- `ownership`
- `session_id`
- `provider_session_id`
- `external_correlation_key`
- `source`
- `event`
- `observed_at_wall`
- `observed_at_monotonic_ms`
- `clock_skew_ms`
- `payload`

Recommended source labels:

- `harness`
- `provider_process`
- `provider_transcript`
- `codex_bridge_state`
- `claude_channel_state`
- `local_health`
- `engine_status`
- `engine_status_file`
- `hook_outbox`
- `session_phase_state`
- `runtime_ingest_events`
- `process_scan_snapshot`
- `managed_sessions_snapshot`
- `hosted_db`
- `hosted_api`
- `timeline_api`
- `timeline_sse`
- `browser_card`
- `ios_timeline`

`browser_card` and `ios_timeline` are client-layer sources. Browser-card
measurement is already useful, but it must be reported separately from timeline
REST and SSE. In warm realtime profiles, `sse_to_paint_ms` is a diagnostic
tiebreaker: it tells whether the backend stream was late or the browser failed
to react quickly to an already-delivered event.

Recommended event labels:

- `launch_requested`
- `session_id_observed`
- `provider_process_observed`
- `managed_state_observed`
- `timeline_card_observed`
- `prompt_sent`
- `prompt_persisted_local`
- `prompt_persisted_hosted`
- `assistant_response_local`
- `assistant_response_hosted`
- `runtime_working`
- `runtime_idle`
- `shutdown_requested`
- `provider_process_gone`
- `local_health_closed`
- `hosted_runtime_closed`
- `timeline_card_closed`
- `mismatch_detected`

## Report Shape

Each run should produce:

- raw JSONL observations
- a compact Markdown summary
- provider transcript path
- local-health snapshots
- hosted debug snapshot
- timeline API snapshots
- failure classification

Failure classifications:

- `provider_latency`
- `hook_latency`
- `engine_heartbeat_lag`
- `hosted_ingest_lag`
- `runtime_state_mismatch`
- `timeline_api_mismatch`
- `timeline_sse_mismatch`
- `browser_render_mismatch`
- `ownership_mismatch`
- `process_truth_mismatch`
- `unknown`

The summary should lead with the trust verdict:

- `pass`: all user-visible state matched raw provider truth
- `lied`: a user-visible surface showed an unsupported state
- `missing`: a required surface never observed the session/event
- `inconclusive`: provider or harness setup failed before testing Longhouse
- `lagged`: state became correct, but propagation exceeded a defined budget

## Budget Promotion

The profiler starts as measurement and becomes a gate only after enough clean
baseline runs exist for each profile class. A single sub-second run proves the
path exists; it does not prove the SLA distribution.

Budget policy by stage:

- PR/local fast gates enforce correctness only: reducer behavior, report
  parsing, fixture-backed browser semantics, and fidelity checks that do not
  require live providers.
- Manual/pre-push profiler runs report latency and classify failures, but
  latency is warn-only unless a profile has already been promoted.
- Nightly dogfood owns p50/p95 trends and should be the first place absolute
  latency budgets become hard alerts.
- Release/ship checks may run one hosted warm live-output and one hosted warm
  close probe, but should gate only on gross regressions and fidelity failures
  until the nightly distribution is boring.

Never gate per-PR on cold timeline paint, hostile SSH/sleep cases, or absolute
p95 from a single run.

Do not hide slow propagation behind vague labels. If a card is stale, detached, offline, or closed, the UI should say that directly.

## Harness Design

Proposed command:

```bash
scripts/ops/profile-managed-session-propagation.py \
  --provider codex \
  --ownership managed \
  --shutdown graceful \
  --subdomain example-tenant \
  --project zerg
```

Useful options:

- `--provider codex|claude|all`
- `--profile cold-timeline|warm-create|warm-live|warm-close|durable-archive|honest-degradation|fidelity`
- `--profile-class cold_timeline|warm_realtime|durable_archive|honest_degradation|fidelity`
- `--ownership managed|unmanaged|all`
- `--shutdown graceful|codex-bridge-stop|claude-exit|terminal-close|wrapper-term|provider-term|provider-kill|ssh-exit|ssh-disconnect`
- `--subdomain <tenant>`
- `--project <label>`
- `--name-prefix <prefix>`
- `--output-dir <path>`
- `--dry-run`
- `--keep-session-open`
- `--no-browser`

The harness should use unique nonce prompts:

```text
Reply with exactly LH_PROBE_<provider>_<ownership>_<run_id>
```

The nonce allows local and hosted transcript matching without fuzzy summary logic, but matching should be substring-based. Exact model compliance is not the metric. When possible, prefer a deterministic tool/action probe over an exact natural-language response.

Hosted snapshots should use the existing canonical debugger:

```bash
scripts/ops/hosted-session-debug.sh --subdomain <subdomain> --session <session-id> --limit 20 --json
```

## Instrumentation Choices

The profiler owns a Longhouse-native observation schema. External tooling can help collect or visualize data, but must not replace the domain vocabulary in this spec.

### Terminal Lifecycle Control

Phase 1 graceful shutdowns can use direct subprocess and signal control. Phase 2 hostile shutdowns and Phase 3 SSH scenarios require a real PTY so that SIGHUP, controlling-TTY loss, and line-discipline signal delivery are faithful.

Use a small `pexpect`/Expect-style wrapper for:

- closing a terminal relationship
- sending `Ctrl-C` through the terminal line discipline
- launching provider TUIs that behave differently under a real TTY
- later SSH disconnect simulations

The harness should record PTY master/slave identity and controlling-TTY state under the `harness` source. It must not treat `SIGTERM` to a provider PID as equivalent to closing a terminal window; those are different facts and the profiler exists to distinguish them.

Do not adopt a larger PTY library until it has been checked for maintenance quality and CI behavior. A narrow local wrapper is acceptable for the first implementation.

### Timeline SSE Measurement

The profiler should measure `timeline_sse` with a direct SSE client against the same endpoint the browser uses. The client records every event with wall and monotonic timestamps and stops when the target session reaches a terminal state or the run times out.

Do not use a headless browser as the SSE clock. Browser rendering adds cold-start
cost, layout work, and client-state flake that are not part of the SSE contract.
Browser-rendered card timing is a separate `browser_card` layer and must not be
conflated with `timeline_sse` latency.

### Deferred External Exports

Keep the primary artifact as Longhouse-native JSONL. OpenTelemetry or OTLP export can be added later as a derived projection if there is an actual trace viewer or observability backend consuming it. The derived export must not rename or flatten away fields like `execution_home`, `managed_transport`, `host_reattach_available`, `source`, or `event`.

For browser-card assertions, prefer the existing Zerg UI capture/fixture
workflow for CI correctness and keep live-browser automation focused on the
small number of hosted profiling paths that need real end-to-end timing.

## CI Path

CI should arrive in layers.

1. **Unit tests for measurement parsing.**
   Validate JSONL artifact parsing, report generation, and mismatch classification.

2. **Captured-artifact replay.**
   Replay captured real-provider rollouts, Claude transcript fragments, hook outbox events, process snapshots, and managed-session heartbeats. This can run in normal CI and avoids pretending a fake provider is realistic.

3. **Local runtime integration.**
   Run against a local Runtime Host and Machine Agent with captured artifacts and, later, deterministic provider stubs. This verifies the Longhouse contract without external provider cost or flake.

4. **Dogfood/nightly real-provider probe.**
   Run real Codex and Claude probes on a controlled machine with explicit credentials and cost controls. Treat this as monitoring, not per-PR CI.

5. **Hosted smoke gate.**
   Once stable, run a narrow managed Codex happy-path probe against a hosted tenant before release/deploy promotion.

## Open Questions

- Can Claude channel launch be made safe and deterministic enough for non-interactive CI?
- Which user-visible card states are acceptable during machine offline windows?
- Should runtime terminal events be durably spooled before they count as meeting
  the managed close SLA under flaky network conditions?

## Built So Far

The first useful profiler exists for Codex and should be kept working while new
profiles are added. It currently covers:

- managed Codex launch, prompt, live output, timeout classification, and bridge stop
- unmanaged Codex launch and observation
- hosted DB/API/timeline REST/timeline SSE measurement
- browser card observation for cold-ish page load and close paint
- provider precondition classification for Codex hooks needing review
- `--profile warm-live` for managed Codex with browser-card and SSE warm
  preconditions before prompt send
- case-aware CI selection through `config/session-propagation-sla.toml`; managed
  Codex is the gate, and runnable Codex experimental paths are report-only
- Markdown and JSON metrics artifacts with trust verdicts

## Next Implementation Slice

Fix the managed Codex live lane exposed by the warm-live profile.

The first warm-live run that reached warm-ready showed:

- browser card ready before prompt send
- timeline SSE stream ready before prompt send
- Codex provider response local in ~2s
- no managed transcript preview on timeline SSE or browser card
- transcript archive arrived later through the outbox/shipper path

Required behavior for the next slice:

1. Add or repair a bridge-emitted transcript progress event for managed
   Codex turns.
2. Timestamp the local bridge emission separately from harness polling.
3. Ensure Runtime Host materializes that event into the provisional transcript
   ledger.
4. Ensure `/api/timeline/sessions/stream` emits a `session_upsert` for that
   transcript preview before durable archive ingest completes.
5. Keep the browser metric split as `bridge -> SSE`, `SSE -> paint`, and
   `bridge -> paint`.
6. Re-run `--profile warm-live` after the bridge/live-lane change.

Non-goals for this slice:

- no Claude profile
- no provider abstraction
- no p95 aggregation
- no hostile SSH or network cases
