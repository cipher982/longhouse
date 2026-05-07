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

Version 1 should measure raw provider truth, machine truth, reducer inputs, runtime truth, timeline REST, and timeline SSE. Browser render and iOS render are later layers after the cheaper surfaces are stable.

## Goals

- Measure propagation from local terminal events to Runtime Host state and timeline cards.
- Separate provider latency from Longhouse telemetry latency.
- Verify managed and unmanaged session lifecycle truth for Codex and Claude.
- Establish a regression harness that can start local and later move into CI or nightly dogfood monitoring.
- Produce per-run artifacts that are useful for debugging without reading full transcripts.
- Make user-visible trust failures explicit: stale running labels, missing cards, delayed close, wrong managed/unmanaged ownership, and mismatched active/idle/closed state.

## Non-Goals

- Do not benchmark model quality or provider response speed as a Longhouse metric.
- Do not require SSH, mobile, browser-render, or network-loss scenarios in the first version.
- Do not collapse Codex and Claude into one implementation path. The providers have different managed mechanics.
- Do not use hidden fallbacks to make the profiler pass.

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

Lifecycle truth is eventful on the primary path and inferred only on backstop paths. A managed control path that knows it is closing must push a terminal lifecycle event immediately; the Runtime Host must not wait for the next heartbeat, process scan, lease expiry, or transcript import to discover that fact.

| Lane | Point Of Truth | Terminal Signal | Target Budget | Backstop Budget | Notes |
| --- | --- | --- | --- | --- | --- |
| Managed Codex bridge | `longhouse-engine codex-bridge` daemon | `terminal_signal` from `codex_bridge` with `terminal_state=session_ended`, `terminal_reason=bridge_stop` | p50 < 250ms, p95 < 1000ms after graceful stop is requested | heartbeat missing lease < 2min until replaced | The bridge already owns API credentials and posts runtime phase/progress events, so graceful stop is a push event. |
| Managed Claude channel | Claude native channel/control wrapper | `terminal_signal` from channel/control path with `terminal_reason=channel_closed` or `provider_exit` | p50 < 250ms, p95 < 1000ms after provider exit is observed by the wrapper/channel | process scan / missing lease < 2min until replaced | Claude must not borrow Codex bridge semantics; it needs its own channel/process lifecycle source. |
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
   Add the equivalent channel/control-path terminal event for managed Claude. Its implementation should follow Claude's actual process/channel mechanics, not Codex bridge state.

4. **Unmanaged close improvements.**
   Add a best-effort provider exit hook/outbox event where available, then tighten the scanned unmanaged backstop separately. The product should communicate that scanned unmanaged close is less immediate than managed close.

5. **Profiler hardening.**
   Fix SSE measurement, record terminal source/reason directly, add SLA verdicts per lane, and classify backstop closures as failures for managed graceful shutdown cases.

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
- bridge state under `~/.claude/managed-local/codex-bridge/`
- rollout JSONL under `~/.codex/sessions/...`
- TUI logs under `~/.codex/log/codex-tui.log`
- `longhouse-engine codex-bridge send`
- `longhouse-engine codex-bridge stop`

Codex is the easiest first target because managed sessions can be launched with `--no-attach`, then driven through the bridge.

The profiler should treat rollout JSONL as provider transcript truth. Bridge `last_turn_status` can be stale; a stale bridge field is a bridge-state finding, not proof the provider turn is still running. If an app-server child survives with no bridge `.sock` or `.json`, classify it as an orphan/cleanup failure.

### Claude

Managed Claude uses:

- `longhouse claude`
- Claude native channel/MCP/stdin control
- channel state under `~/.claude/channels/longhouse/sessions/`
- `longhouse claude-channel send`
- `longhouse claude-channel interrupt`
- process scan for liveness

Claude requires an actual channel-backed Claude process. A headless `--no-attach` launch prepares state, but does not by itself create the provider process to measure.

Claude liveness is noisier because it depends heavily on process scan plus channel state. The profiler should record the channel state file and process identity every time it classifies Claude as live, detached, or closed.

## Measurements

Every run should write a structured JSONL artifact. Each observation must include:

- `harness_version`
- `run_id`
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

`browser_card` and `ios_timeline` are later-layer sources. Version 1 should record timeline REST and SSE before adding full client rendering.

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

## Budgets

The first implementation should record timings without hard fail thresholds. It should not emit `lagged` until budgets are explicitly defined. Before that, slow-but-correct runs should be reported as `pass` with timing warnings.

After enough clean local baseline runs, promote measured budgets into gates.

Suggested future budget classes:

- **interactive**: user-visible changes that should feel immediate
- **fresh**: changes acceptable after a short backend round trip
- **eventual**: archive or catch-up state that is useful but not live-control critical

Do not hide slow propagation behind vague labels. If a card is stale, detached, offline, or closed, the UI should say that directly.

## Harness Design

Proposed command:

```bash
scripts/ops/profile-managed-session-propagation.py \
  --provider codex \
  --ownership managed \
  --shutdown graceful \
  --subdomain david010 \
  --project zerg
```

Useful options:

- `--provider codex|claude|all`
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

Version 1 should measure `timeline_sse` with a direct SSE client against the same endpoint the browser uses. The client records every event with wall and monotonic timestamps and stops when the target session reaches a terminal state or the run times out.

Do not use a headless browser to measure SSE in version 1. Browser rendering adds cold-start cost, layout work, and client-state flake that are not part of the SSE contract. Browser-rendered card timing is a later `browser_card` layer and must not be conflated with `timeline_sse` latency.

### Deferred External Exports

Keep the primary artifact as Longhouse-native JSONL. OpenTelemetry or OTLP export can be added later as a derived projection if there is an actual trace viewer or observability backend consuming it. The derived export must not rename or flatten away fields like `execution_home`, `managed_transport`, `host_reattach_available`, `source`, or `event`.

When browser-card measurement is added, prefer the existing Zerg UI capture/fixture workflow before introducing broad live-browser automation. Only escalate after provider, machine, runtime, timeline REST, and timeline SSE layers are boring.

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

- Should managed graceful shutdown emit an explicit terminal lifecycle event, or is process absence enough?
- Should timeline SSE failures fail the first gate, or stay measured-only until the stream path stabilizes?
- Can Claude channel launch be made safe and deterministic enough for non-interactive CI?
- Which user-visible card states are acceptable during machine offline windows?

## First Implementation Slice

Build the smallest useful profiler:

1. Codex managed graceful shutdown.
2. Codex unmanaged graceful shutdown.
3. Hosted API/DB/timeline REST/SSE measurement.
4. Local process and transcript measurement.
5. Markdown report with trust verdict.

Only after that passes should Claude and hostile shutdown modes be added.
