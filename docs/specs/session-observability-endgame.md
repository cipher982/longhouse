# Session Observability Endgame

Status: Draft
Last updated: 2026-04-23

## Why This Exists

Longhouse is mission control for real agent sessions running on user-owned machines.

If a power user says "managed Claude feels slow," Longhouse should be able to answer that from telemetry, not from log archaeology and intuition.

This spec defines the end-state observability contract for that product loop.

## Product Questions We Must Answer Fast

For any reported slowdown, within a few minutes we should be able to say:

- provider/model latency got worse
- Longhouse runtime overhead got worse
- engine ship/export got worse
- ingest decode/write got worse
- the session is bloated and upstream inference is slow
- the machine is unhealthy or offline

If we cannot answer those questions quickly, we are not fully observed.

## End-State Success Criteria

The system is "fully observed" only when all of these are true.

### Coverage

- Every managed turn emits a root runtime-host trace with phase timings.
- Every engine ship/export cycle emits machine-local timing telemetry.
- Every `/api/agents/ingest` request emits both traces and metrics.
- Every `/api/agents/heartbeat` request emits both traces and metrics.
- Every slow-turn report can be matched to a machine, session, provider, and build identity.

### Debuggability

- For at least 95% of managed turns, we can decompose wall time into:
  - runtime-host dispatch
  - provider wait/send
  - active/terminal observation delay
  - engine ship/export
  - ingest decode/write
- For at least 99% of turns above the slow threshold, a trace or status snapshot exists with enough detail to identify the dominant phase.
- A machine-facing route can answer "is this machine's shipping path healthy right now?" from latest heartbeat data without opening logs first.

### Alertability

- We have a dashboard that shows `p50/p95/p99` for managed turn dispatch, active wait, terminal wait, ingest decode, ingest write, and engine ship latency.
- We alert on sustained regressions before users need to report them.
- We can split every alert by provider, managed/unmanaged, build identity, and machine.

### Privacy

- Prompt/response bodies are excluded by default.
- Tool input/output bodies are excluded by default.
- Telemetry exports only IDs, counts, phases, durations, sizes, outcome classes, and build metadata.
- Rich payload capture is sampled, short-lived, and explicitly opt-in.

## Canonical Signals

### Traces

Traces are for per-turn and per-request causality.

Required roots:

- `longhouse.turn`
- `longhouse.ingest`
- engine ship root spans once engine OTLP is live

### Metrics

Metrics are for dashboards, SLOs, and alerting.

Required families:

- managed turn request counts by provider and outcome
- managed turn dispatch latency histograms
- managed turn active/terminal wait histograms
- ingest request counts by auth kind, provider, and status
- ingest decode/write latency histograms
- ingest payload size and event count histograms
- heartbeat request counts by auth kind, last ship result, and status
- heartbeat write latency and payload size histograms
- engine ship attempt counts by outcome
- engine ship latency histograms or rolling status summaries
- heartbeat freshness and offline duration

### Status Snapshots

Status snapshots are for machine-local diagnosis when traces are absent or export is down.

Required surfaces:

- `~/.longhouse/agent/engine-status.json`
- `local-health` JSON
- server heartbeat rows
- `GET /api/agents/machines/health`

These must carry enough data to diagnose local transport issues without tailing logs.

## Dashboards

Launch-minimum dashboards:

1. Managed Turn Latency
   - `p50/p95/p99` send-accept
   - active wait timeout rate
   - terminal wait timeout rate
   - split by provider, build, machine

2. Ingest Health
   - request rate
   - invalid payload rate
   - decode latency
   - write latency
   - payload bytes
   - events per request

3. Machine Shipping Health
   - ship success/failure rate
   - connect error rate
   - rate-limit rate
   - rolling ship latency
   - heartbeat freshness
   - spool backlog and dead-letter counts

Current gap:

- handler-level ingest metrics do not see dependency-level auth rejections yet because FastAPI can reject on `Depends(...)` before the route body runs. Until middleware accounting lands, treat `agents_ingest_requests_total` as "requests that entered the handler," not "all requests that hit the path."

## Alert Thresholds

Initial thresholds should be simple and obvious, not clever.

- managed turn send-accept `p95` above 3x trailing 7-day baseline for 30 minutes
- active or terminal watcher timeout rate above 5% for 15 minutes
- ingest write `p95` above 2s for 15 minutes
- engine connect error rate above 10% for 15 minutes
- heartbeat missing for more than 2 intervals on machines that were recently active

These are starting points. Real thresholds should be adjusted from dogfood data.

## Rollout Phases

### Phase 1

- runtime-host manual traces
- runtime-host Prometheus metrics
- opt-in OTLP export

### Phase 2

- engine ship outcome and latency telemetry in heartbeat/status snapshots
- local-health exposure of those fields
- server-side machine health summary route over latest heartbeat state

### Phase 3

- engine OTLP traces
- trace context propagation across runtime-host, engine, and ingest boundaries where possible

### Phase 4

- dashboards and alerts wired to dogfood and hosted environments
- slow-turn regression playbook

## Done Means

We are done when a future "managed sessions feel slow" report can be answered from telemetry in one pass, without opening local logs first.

Near-term slice success criteria:

- `GET /api/agents/machines/health` returns one latest heartbeat-derived row per device.
- Each row includes heartbeat freshness, dominant transport reason, rolling ship outcomes, and backlog/dead-letter signals.
- Operator flows can filter that surface by device and derived state without querying raw heartbeat history directly.
- `GET /api/agents/sessions/{session_id}/turns` and turn detail responses expose derived phase durations from canonical turn timestamps instead of forcing trace inspection for basic timing decomposition.
