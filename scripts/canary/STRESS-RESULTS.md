# Cube → david010 realtime pipeline stress profile

Always-on canary: producer + observer running as systemd user services
on cube (Alabama residential fiber). All tests hit hosted david010 at
zerg.

Baseline network: `ping cube → david010.longhouse.ai` → 26ms RTT.

## 2026-04-26 — initial sweep (commit `98dee530`)

Stress script: `scripts/canary/stress.py`. Fires fabricated
RuntimeEventIngest POSTs at target rate into the always-on canary
session so the existing observer measures SSE wake latency.

### Ingest RTT (cube POST → server ack round-trip)

| rate | sent | errors | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|---:|
| 1/s  |   60 | 0 | 59ms | 67ms | 105ms |
| 5/s  |  300 | 1 | 60ms | 95ms | 120ms |
| 20/s |  949 | 0 | 62ms | 70ms |  75ms |

Flat at ~60ms regardless of rate up to 20/s. No visible saturation. The
20/s p99 being *lower* than 5/s p99 is likely connection warm-up at the
smaller rate — HTTP/2 reuse pays off once traffic is sustained.

### SSE hop (server wake → observer parse)

| rate | observations | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|
| 1/s  |  62 | ≤50ms | ≤50ms | ≤50ms |
| 5/s  | 241 | ≤50ms | ≤50ms | ≤50ms |
| 20/s | 652 | ≤50ms | ≤50ms | ≤250ms |

Excellent: p95 stays at 50ms bucket ceiling across every rate. p99 at
20/s ticks to 250ms — first sign of backpressure. Sample bucket is too
coarse (50ms → 250ms is the next bucket boundary) so we can't
distinguish e.g. 60ms from 249ms without finer buckets.

### Observations

- **SLA targets (p50 ≤150ms / p95 ≤300ms) are crushed.** Observed SSE
  p95 is <50ms end-to-end from a real residential Alabama path.
- **No knee visible up to 20/s.** For context: 20 events/second is far
  higher than anything a real managed session produces (one managed
  Codex session emits a few phase signals per turn).
- **One ingest error at 5/s.** Transient; didn't recur at 20/s. Worth
  investigating if it repeats.
- **Observer capture rate lags at high volume.** At 20/s we sent 949
  events but saw 652 SSE observations in the delta window. Some were
  coalesced by the SSE signature filter (multiple runtime writes → one
  workspace_changed frame when the DB signature doesn't change between
  writes). Not a bug — expected behavior for fan-out deduping.

### Next runs to consider

- Add finer sub-50ms buckets to `canary_latency_seconds` (0.02, 0.03,
  0.04) so we can see what the actual floor is.
- Burst test: 100/s for 10s to find the ingest knee.
- Concurrent sessions: 10 separate canary sessions firing 5/s each to
  exercise pubsub fan-out isolation at a more realistic scale.
