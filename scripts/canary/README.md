# Longhouse realtime canary

Always-on synthetic probe that validates the full pipeline:

```
producer  ──POST──> /api/agents/runtime/events/batch
                          │
                          ├── server ingest     (measured by producer → hop=ingest)
                          ├── pubsub publish
                          └── SSE stream fan-out
                                    │
                                    └── observer receives workspace_changed
                                                 (measured by observer → hop=sse)
```

The producer + observer each POST a `CanaryObservation` back to
`/api/telemetry/canary-observation`, which records into the
`canary_latency_seconds{hop,surface}` histogram and updates the
`canary_seq_last_seen{hop}` gauge.

## Running the producer

Needs a device token that can hit `/api/agents/runtime/events/batch`
(agents_token auth) **and** admin credentials for the observation endpoint.

```bash
export LONGHOUSE_CANARY_URL=https://david010.longhouse.ai
export LONGHOUSE_CANARY_TOKEN=<agents-device-token>
export LONGHOUSE_CANARY_ADMIN_COOKIE=<longhouse_session cookie for admin>
python3 scripts/canary/producer.py
```

The producer persists its session UUID to `~/.longhouse/canary-session-id`
so it keeps ingesting into the same session across restarts.

## Running the observer

```bash
export LONGHOUSE_CANARY_URL=https://david010.longhouse.ai
export LONGHOUSE_CANARY_ADMIN_COOKIE=<longhouse_session cookie>
python3 scripts/canary/observer.py
```

The observer reads the canary session id from
`~/.longhouse/canary-session-id` (must be populated by the producer first).

## What good looks like

After both are running for ~5 min, hit `/metrics` on the server and grep
for `canary_`:

- `canary_seq_last_seen{hop="ingest"}` and `{hop="sse"}` advance together
- `canary_latency_seconds_count{hop="sse"}` grows
- p50 on `canary_latency_seconds{hop="sse"}` should be well under 1s on a
  healthy deployment

If `canary_seq_last_seen{hop="sse"}` falls behind `{hop="ingest"}`, pubsub
fan-out is broken. If `hop="ingest"` stops advancing, server ingest is down.
