# Machine Agent Flight Recorder

## Goal

Give dogfood installs a local, low-overhead way to diagnose Machine Agent stalls and degraded shipping without guessing from UI symptoms.

The recorder answers:

- did work wait in the local scheduler, spool, or hook outbox?
- did parsing/compression finish quickly?
- did the HTTP ingest call hang or fail?
- was the control WebSocket healthy at the same time?
- was the local process CPU, memory, or disk state unusual?

## Non-Goals

- no remote telemetry collector
- no OpenTelemetry redesign
- no transcript payload capture
- no prompt, tool body, response body, or compressed request bytes
- no user-facing UI in v1

## Activation

The recorder is opt-in for local dogfooding:

```bash
LONGHOUSE_ENGINE_FLIGHT_RECORDER=1 longhouse-engine connect
```

Records are written under `~/.longhouse/agent/flight-recorder/` by default. Set `LONGHOUSE_ENGINE_FLIGHT_RECORDER_DIR` to override the directory.

## Records

`ship_trace.v1` records one metadata-only row per ship attempt. It includes local path, provider, session id, byte offsets, event count, queue timing, parse/prepare timing, HTTP start/finish timestamps, HTTP latency, status, outcome, and retry decision.

`flight_sample.v1` records a 5-second sample while the Machine Agent is running. It includes hook outbox count/age, spool pending/dead counts, process CPU/RSS counters, disk free bytes, control channel snapshot, recent ship stats, and local scheduler backlog.

`flight_event.v1` records lifecycle events such as recorder startup.

Files rotate daily as JSONL and are pruned after 7 days.

## Success Criteria

- A degraded shipping episode can be classified from local JSONL without re-running the repro.
- A control WebSocket outage can be distinguished from data-plane ingest backpressure.
- The recorder adds no network traffic and does not block shipping when the recorder writer falls behind.
- Flight records contain no transcript payloads, prompt text, tool bodies, compressed bytes, or server response bodies.
