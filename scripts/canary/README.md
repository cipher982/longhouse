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
`/api/telemetry/canary-observation`. Results land in
`canary_latency_seconds{hop,surface}` (histogram) and
`canary_seq_last_seen{hop}` (gauge) on `/metrics`.

## Tokens

Two separate tokens:

- **`LONGHOUSE_AGENTS_TOKEN`** — a normal agents device token. Authenticates
  the producer's POST to `/api/agents/runtime/events/batch` (ingest).
  Generated the same way as any machine registration.
- **`LONGHOUSE_CANARY_TOKEN`** — a shared secret set as an env var on the
  Longhouse server (`LONGHOUSE_CANARY_TOKEN=...`). Gates the canary
  observation endpoint and the canary-only SSE endpoint. Pick any random
  string; set it in the server env and hand the same value to the probes.

## Deploy on cube (Alabama residential) — recommended

From your laptop:

```bash
ssh cube \
  "LONGHOUSE_CANARY_URL=https://david010.longhouse.ai \
   LONGHOUSE_AGENTS_TOKEN=<device-token> \
   LONGHOUSE_CANARY_TOKEN=<shared-secret> \
   LONGHOUSE_SLA_WEBHOOK=https://ntfy.sh/lh-sla \
   bash -s" < scripts/canary/install_cube.sh
```

Creates three systemd user services (`longhouse-canary-producer`,
`-observer`, `-sla-watch`), enables user linger so they survive logout,
and starts them.

Check logs:
```bash
ssh cube 'journalctl --user -u longhouse-canary-observer -f'
```

Stop / restart:
```bash
ssh cube 'systemctl --user restart longhouse-canary-producer'
ssh cube 'systemctl --user stop longhouse-canary-observer'
```

## Run ad-hoc (anywhere with Python + httpx)

```bash
export LONGHOUSE_CANARY_URL=https://david010.longhouse.ai
export LONGHOUSE_AGENTS_TOKEN=<device-token>
export LONGHOUSE_CANARY_TOKEN=<shared-secret>

python3 scripts/canary/producer.py &
python3 scripts/canary/observer.py &
LONGHOUSE_SLA_WEBHOOK=https://ntfy.sh/lh-sla \
    python3 scripts/canary/sla_watch.py &
```

Producer persists its session UUID to `~/.longhouse/canary-session-id`
so probes across restarts aggregate into the same session.

## What good looks like

After 2-3 minutes of runtime, on the server:

```bash
curl -sS https://david010.longhouse.ai/metrics | grep canary_
```

You should see:
- `canary_seq_last_seen{hop="ingest"}` and `{hop="sse"}` both non-zero
  and advancing together. A gap > 10 between them means the observer is
  falling behind, which means pubsub/SSE is dropping events.
- `canary_latency_seconds_count{hop="sse"}` grows by ~2/minute.
- `canary_latency_seconds_bucket{hop="sse",le="0.5"}` captures the bulk
  of samples on a healthy pipeline.

## Selfcheck

One-shot health JSON (admin or canary-token auth):

```bash
curl -sS -H "X-Canary-Token: <shared-secret>" \
  https://david010.longhouse.ai/api/telemetry/selfcheck
```

Returns `{"ok": true, "hops": {...}, "seq": {...}}`. `ok=false` means at
least one required hop (`ingest`, `sse`) is dead or the producer↔observer
seq gap exceeded 10.

Cron this from anywhere, post to a webhook on `ok=false`.
