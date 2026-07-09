# DB Load Observability (Measure Epic)

**Status:** Approved plan, not started
**Owner:** Longhouse core
**Created:** 2026-07-09
**Related:** `docs/specs/sqlite-data-plane-completion.md`,
`docs/specs/reliability-data-plane.md`,
`docs/specs/hot-cold-runtime-reliability-hardening.md`

## Executive Summary

Longhouse hosted has good instantaneous instrumentation and zero retained
measurement. The Runtime Host exports a real Prometheus `/metrics` endpoint,
WriteSerializer per-label stats (queue wait, exec time, counts), WAL checkpoint
results, and archive-pressure payloads — but nothing on the zerg host scrapes
or stores any of it. Every number evaporates unless someone is watching.

Consequence: months of SQLite firefighting (163 reliability commits since
April) were flown on symptoms and one-off snapshots. We cannot rank fixes by
measured impact, attribute disk IOPS/CPU to the david010 container versus its
neighbors, or prove the upcoming Phase E reclaim helped beyond "feels better."

This epic ships the smallest capture layer that fixes that, then soaks it for a
clean baseline **before** the Phase E reclaim runs. The reclaim is the largest
state change this database will ever see; the "before" data cannot be captured
afterward.

Deliberately NOT in scope: Grafana, dashboards, alerting rules, a full
observability stack. Zero-users solo-dev rules apply — ndjson on disk plus
ad-hoc analysis is enough until proven otherwise.

## What Exists Today (verified 2026-07-09)

- `server/zerg/metrics.py` + `routers/metrics.py`: Prometheus counters,
  gauges, histograms; trusted-request gated.
- `/api/health` (trusted): WriteSerializer metrics per label, live-writer
  metrics, archive WAL pressure, projection/enrichment lag.
- WAL checkpoint loop results per store (`database.py`), bounded
  `PRAGMA optimize` maintenance.
- Flight recorder + `scripts/ops/flight-recorder-percentiles.py`: laptop-side
  ship-trace latency only, not hosted DB load.
- On the zerg host: no prometheus/grafana/victoria/netdata/telegraf containers.
  Nothing retains anything.
- `longhouse.db.table-bytes.json` sampler times out (180s) on the 126GiB
  monolith — in-DB size attribution is currently impossible.

## Plan

### Phase 1 — Capture jobs (~one session)

Two small samplers on the zerg host, both writing ndjson with UTC timestamps
to a local data dir (rotated, weeks of retention is plenty):

1. **Runtime metrics snapshotter.** Every 1–5 min, curl the david010
   `/metrics` endpoint and the trusted health payloads; append parsed ndjson.
   Retains: serializer label counts (which write lanes dominate), queue-wait /
   exec-time distributions, WAL bytes, checkpoint durations, archive pressure,
   projection lag.
2. **Container resource sampler.** Same cadence: cgroup `io.stat` + `cpu.stat`
   for `longhouse-david010` (and siblings: demo, canary, control-plane) plus
   volume-level `iostat` for `/data`. Answers "is it the DB; reads or writes;
   which tenant" with zero app changes.

Delivery mechanism: Sauron-style cron job or plain host cron — whatever is
fastest; this is ops tooling, not product. Keep it out of the runtime image.

### Phase 2 — Baseline soak (5–7 days, no work)

Let capture run against the untouched 126GiB monolith under real dogfood load.
**Freeze all DB-behavior changes during the soak** so the baseline is clean.
Safe exception: observability-only work (e.g. outbox oldest-pending alerting
from the hardening spec).

### Phase 3 — Read the baseline, hand off

One analysis pass producing the numbers the completion epic consumes:

- top serializer labels by count and by total exec time;
- queue-wait percentiles per lane, WAL size envelope, checkpoint cost;
- david010 IOPS/CPU share vs neighbors; read/write split;
- any surprise (a lane or query class nobody suspected).

## Acceptance

- Both samplers running unattended for 7 consecutive days with no gaps longer
  than one cadence interval.
- The baseline analysis exists and ranks write lanes / resource consumers.
- Post-reclaim, the identical capture proves the before/after delta for
  `docs/specs/sqlite-data-plane-completion.md` step 1, and stays running as
  permanent cheap telemetry.

## Non-Goals

- No dashboards, no alerting stack, no VictoriaMetrics/Prometheus server
  (revisit only if ndjson analysis becomes the bottleneck).
- No new in-app instrumentation unless the baseline shows a blind spot.
- No laptop/self-host capture — hosted david010 is the whale and the target.
