# Longhouse observability stack (god view)

Opt-in Prometheus + Grafana that scrapes the runtime's `/metrics` and renders a
unified dashboard across the instance and every device.

The runtime already emits the metrics; this stack just collects and retains
them. It is kept separate from `docker/docker-compose.dev.yml` so `make dev`
stays lean.

## What it shows

- **Live ship path** — `event_age_at_ingest` p50/p95 (engine→server hop) and
  ingest throughput by provider.
- **Server write pressure** — WriteSerializer queue wait/exec percentiles per
  label, queue depth, and SQLite WAL bytes (previously `/health`-JSON only).
- **Per-device shipping** — ship latency, spool pending/dead, archive backlog,
  heartbeat age, offline count, consecutive failures (previously latest-only in
  the `agent_heartbeats` table).

## Run it

1. The runtime gates `/metrics`. Set a token on the runtime:

   ```bash
   export LONGHOUSE_METRICS_TOKEN=$(openssl rand -hex 16)
   longhouse serve   # or set it in the dev compose backend env
   ```

2. Bring up the stack with the same token so Prometheus can authenticate:

   ```bash
   LONGHOUSE_METRICS_TOKEN=$LONGHOUSE_METRICS_TOKEN make observability-up
   open http://localhost:3001   # Grafana — admin / ${GRAFANA_ADMIN_PASSWORD:-admin}
   ```

   The "Longhouse God View" dashboard is provisioned automatically.

`make observability-down` stops it.

## Targeting a different runtime

`RUNTIME_METRICS_TARGET` defaults to `host.docker.internal:8080` (a local
`longhouse serve`). Override for the dev compose backend or a remote host:

```bash
RUNTIME_METRICS_TARGET=host.docker.internal:47300 make observability-up
```

## Traces (later)

OTEL trace export is already implemented on both the server
(`server/zerg/observability.py`) and engine (`engine/src/observability.rs`),
opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`. A Tempo backend is intentionally not
included yet: the engine and server do not currently propagate W3C
`traceparent` across the ship hop, so traces would be disjoint per process.
Add Tempo together with trace-context propagation when end-to-end traces are
worth the wiring.
