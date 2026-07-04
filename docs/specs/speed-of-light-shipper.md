# Speed-of-Light Shipper

Status: Draft
Last updated: 2026-06-03

## Problem

Longhouse's core value is not "eventually import old CLI logs." The core value
is that every agent session call is shipped to the Runtime Host quickly enough
that Longhouse becomes the user's reliable memory and control plane.

The product invariant:

> If the user's machine is awake, the Machine Agent is running, and the Runtime
> Host is reachable, every newly appended session event should reach Longhouse
> inside a sub-10-second SLA.

Archive repair is allowed to be large. It is not allowed to make the live
shipping path ambiguous, idle, or slow.

The June 2026 dogfood backlog exposed two failures:

- ready local backlog rows were stranded behind stale `next_retry_at` values
- the engine could go idle between periodic spool ticks even while eligible
  backlog remained

Those bugs were fixed in `bc45660a`, but the target design should be stronger:
a work-conserving, lane-aware shipper that can saturate safe throughput while
protecting live latency.

## Goals

- Ship live transcript appends with p95 append-to-host-ack under 10 seconds.
- Target p95 append-to-HTTP-send under 1 second for local engine work under
  normal load.
- Keep the archive lane work-conserving: no sleeping while eligible archive
  work exists and the host is reachable.
- Protect live lane capacity even during multi-GB archive repair.
- Drain backlog by bytes and event throughput, not only by path/range count.
- Make bottlenecks visible: local parse, local queue, network, host queue,
  host write, host backpressure, and enrichment lag.
- Preserve raw transcript evidence. Do not insert LLM summarization,
  embedding, or lossy compaction into the hot shipping path.

## Non-Goals

- No LLM calls in the shipper hot path.
- No provider-specific semantic interpretation in the shipper.
- No second durable transcript store on the machine beyond provider logs plus
  pointer/cursor metadata.
- No hidden "make health green by dropping evidence" behavior.
- No execution migration or hidden session ownership changes.

## Lane Model

The shipper has five lanes. Lower lane numbers have strict priority.

| Lane | Name | Purpose | SLA |
| --- | --- | --- | --- |
| L0 | control | control channel, launch readiness, local health | heartbeat-grade |
| L1 | live transcript | new provider appends from active sessions | p95 < 10s append-to-host-ack |
| L2 | live gap repair | current managed/unmanaged session holes | seconds to tens of seconds |
| L3 | archive repair | historical backlog and broad reconciliation | work-conserving, backpressured |
| L4 | enrichment | embeddings, summaries, search side indexes | out of hot path |

Important implications:

- L1 must never wait behind L3.
- L3 can be throttled by the Runtime Host and local limiter, but it should never
  sit idle because a retry timer was stale or a refill tick was missed.
- L4 is downstream of durable ingest. A missing embedding is not a shipping
  failure.

## Speed-of-Light Budget

The live path budget should be tracked as a sum of observable stages:

| Stage | Target p95 | Notes |
| --- | ---: | --- |
| provider append to OS observation | 250ms | FSEvents / wake socket / hook signal |
| observation to scheduler enqueue | 100ms | coalescing and path classification |
| scheduler enqueue to job start | 250ms | live reservation must make this boring |
| parse and batch build | 500ms | bounded batch, no enrichment |
| HTTP connect/send/ack | 2s | normal WAN + host write |
| host write to UI visibility | 1s | SSE/WebSocket/timeline projection |
| slack budget | ~6s | sleep/wake jitter, provider burst, transient queue |

The product SLA is 10 seconds. The engineering target is much tighter so normal
operation feels terminal-class and the 10-second SLA survives noisy machines.

Archive repair has a different budget:

- it should consume all safe leftover capacity after L0-L2
- it should back off immediately on explicit host pressure
- it should recover immediately when pressure drops
- it should optimize bytes/sec and events/sec without raising live queue wait

## Current Bottleneck Taxonomy

Every slow or stalled ship should land in exactly one bucket.

| Bucket | Signal | Owner |
| --- | --- | --- |
| local observation | event not noticed quickly | watcher/wake/hook |
| local scheduling | queued but not started | engine scheduler |
| local parse | file read/parse/batch slow | engine parser |
| local network | connect/TLS/upload slow | machine/network |
| host admission | archive rejected before write | Runtime Host admission |
| host write queue | `X-Ingest-Queue-Wait-Ms` high | Runtime Host WriteSerializer |
| host write exec | SQLite write slow | Runtime Host ingest |
| projection | acked but not visible | server/UI projection |
| enrichment | ingested but not indexed/summarized | downstream jobs |

The menu bar and `longhouse local-health` should expose these buckets directly.
"Pending" alone is not an actionable diagnosis.

## Scheduler Contract

The engine scheduler should be work-conserving and lane-aware.

Required behavior:

- Keep a live reservation. L3 archive jobs cannot consume all worker slots.
- Always pop L1 before L2/L3 when live work is ready.
- Refill L3 immediately when the scheduler drains and eligible backlog remains.
- Do not let future local retries block unrelated eligible backlog.
- Do not let one huge path monopolize the archive lane.
- When host backpressure arrives, lower archive pressure globally, not just for
  one path.
- Use jitter for retry times to avoid synchronized retry storms.

Explicit anti-patterns:

- A periodic timer as the only mechanism that feeds backlog.
- A path selector and path replay using different readiness predicates.
- Count-only budgets that clear 5,000 tiny ranges and then appear "almost done"
  while 16GB remains.
- A global offline state triggered by archive backpressure.

## Runtime Host Contract

The Runtime Host must admit writes by lane.

Required behavior:

- L0/L1 writes get priority over L3 archive repair.
- Archive admission is bounded but not single-file serialized unless the host is
  actually saturated.
- Archive rejection is explicit and cheap: reject before expensive decode when
  possible.
- Every ingest response should include timing feedback:
  - `X-Ingest-Queue-Wait-Ms`
  - `X-Ingest-Exec-Ms`
  - `X-Ingest-Lane`
  - `Retry-After` for retryable backpressure
- Backpressure errors must be typed, stable, and machine-readable.

The server should not try to solve throughput by accepting unbounded archive
requests. It should expose pressure early enough for the engine to find the
safe operating point.

## Archive Drain Strategy

Archive repair should optimize for "usefulness first, then completeness."

Priority order:

1. current active session gaps
2. recent small transcript gaps
3. recent medium transcript gaps
4. older small gaps
5. older medium gaps
6. large gaps
7. huge gaps over 100MB

Within each class, the engine should avoid strict oldest-first behavior if it
would keep useful recent data missing behind old bulk data.

Archive batch sizing should be adaptive:

- small ranges can be coalesced to reduce request overhead
- large ranges should be chunked by byte budget and resumable offsets
- huge files should use explicit chunk progress so the UI does not appear stuck
- the limiter should consider bytes/sec, events/sec, queue wait, and rejection
  rate

## Throughput Controller

The current adaptive limiter uses host queue wait. Keep that, but expand the
controller into a lane-specific throughput controller.

Inputs:

- host queue wait p50/p95
- host exec time p50/p95
- archive 503/backpressure rate
- live ship latency p50/p95
- live enqueue-to-job p95
- archive bytes/sec EWMA
- archive events/sec EWMA
- local CPU saturation
- local parse latency
- upload throughput estimate

Outputs:

- archive concurrency cap
- archive per-request byte cap
- archive path refill batch size
- huge-range eligibility
- retry delay floor/ceiling

Control rules:

- If live p95 approaches SLA, cut archive cap first.
- If host queue wait rises, reduce archive cap multiplicatively.
- If host queue wait is low and live p95 is healthy, increase archive cap.
- If archive requests are small and overhead-bound, coalesce.
- If archive requests are large and timeout-prone, split.
- If backpressure is isolated to huge ranges, quarantine huge ranges rather
  than slowing all small repair.

## Observability Contract

`engine-status.json`, local health, and the menu bar should show enough raw
state to answer "why is it not faster?"

Minimum fields:

- live lane:
  - last live append observed
  - last live HTTP send
  - last live ack
  - live p50/p95 latency
  - live failures by class
- archive lane:
  - pending ranges, paths, sessions, bytes
  - pending by provider
  - pending by size bucket
  - oldest/newest pending
  - active archive workers
  - archive concurrency cap
  - archive bytes/sec and events/sec EWMA
  - host queue wait EWMA
  - backpressure count and last typed reason
  - next eligible retry, not just next retry
- host:
  - write queue depth or busy state
  - ingest queue wait
  - ingest exec time
  - archive admission state
- enrichment:
  - ingest-complete but index-pending count

Menu bar copy should be plain:

```text
Live shipping healthy. Archive repair draining 16.2GB across 3,905 ranges.
Archive is host-throttled at 1 worker; last ship 12s ago; live launch ready.
```

That is much more useful than "needs repair."

## Benchmark and Proof Harness

The shipper needs repeatable proof, not vibes.

### Local Engine Stress

Create a synthetic engine harness that writes valid Claude/Codex/OpenCode logs
into temp provider roots while the real scheduler ships to a local HTTP echo.

Measurements:

- append-to-observation
- observation-to-enqueue
- enqueue-to-job
- parse/build
- job-to-http
- http latency
- ack-to-status

Pass criteria:

- L1 append-to-HTTP-send p95 < 1s
- L1 append-to-host-ack p95 < 10s
- L3 active backlog does not move L1 enqueue-to-job p95 above 250ms

### Hosted Ingest Stress

Run a hosted canary that sends mixed live/archive traffic to a real Runtime Host.

Measurements:

- live ack p95 under archive pressure
- archive bytes/sec
- archive backpressure rate
- WriteSerializer queue wait
- SQLite write exec time

Pass criteria:

- live p95 remains under 10s while archive repair is active
- archive makes forward progress whenever host queue wait is below threshold
- archive backpressure is typed and reflected in machine health within one
  status interval

## Implementation Phases

### Phase 0: Incident Fixes Already Shipped

- Default archive repair mode to drain for dogfood.
- Raise archive budgets from tiny trickle defaults.
- Let archive replay queue behind live writes instead of rejecting whenever the
  writer is non-empty.
- Use consistent ready predicates for path selection and replay.
- Refill archive work immediately when the scheduler drains.

Commit through `bc45660a`.

### Phase 1: Measurement Truth

- Split live and archive counters in `RecentShipStatsTracker`.
- Add live SLA histograms to `engine-status.json`.
- Add archive bytes/sec and events/sec EWMA.
- Add eligible retry count separate from pending retry count.
- Add active worker counts by lane.
- Add menu bar copy that says live healthy vs archive draining.

Success: a user can answer whether wall-clock is parse, network, host queue, or
backpressure from local health alone.

### Phase 2: Work-Conserving Archive Controller

- Replace row-count refill with byte-aware refill.
- Add global archive pressure state fed by backpressure and queue wait.
- Make large/huge ranges explicit queues.
- Coalesce tiny ranges when host queue wait is low.
- Split large ranges when request duration or payload size crosses thresholds.

Success: with live lane idle and host queue wait low, archive workers ramp until
CPU, network, or host queue becomes the limiting factor.

### Phase 3: Live SLA Harness

- Add local engine stress harness.
- Add hosted mixed live/archive ingest canary.
- Gate release on live p95 under archive pressure, not only basic smoke.

Success: CI catches any future regression where archive work delays live
shipping.

### Phase 4: Operator Controls

- `longhouse archive status --watch`
- `longhouse archive speed`
- `longhouse archive drain --target max-safe`
- `longhouse archive pause --class huge`
- `longhouse archive inspect --largest`

Success: power users can see and steer drain behavior without SQLite spelunking.

## Open Design Calls

- Should dogfood default always be `drain`, while packaged user default is
  `normal` with explicit "drain now"?
- Should hosted tenants get a per-tenant archive token bucket independent of
  live write admission?
- Should huge ranges be opt-in by default, or drain automatically only when
  live lane is quiet and the machine is on power?
- Should local health calculate an ETA range once byte/sec stabilizes, or avoid
  ETA until large-range distribution is known?
- Should archive status expose file paths by default, or hide paths unless
  `--verbose` to avoid leaking local project names in casual screenshots?

## Definition of Done

The shipper is "powerful and optimized" when these are true:

- live transcript events hit the Runtime Host within the 10-second SLA under
  archive pressure
- archive repair never idles while eligible work exists and host pressure is low
- archive throughput ramps to the safe bottleneck without manual tuning
- all bottlenecks are visible in local health and menu bar JSON
- historical completeness is measured by bytes, ranges, sessions, providers,
  and age
- no LLM/enrichment step can block raw durable ingest
