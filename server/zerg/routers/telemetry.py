"""Client-side realtime latency telemetry.

Accepts render beacons from web/iOS after they display an event. The
authoritative SLA view is the Prometheus `event_end_to_end_latency_seconds`
histogram (scrape-based); this module also keeps a small in-process deque
for unit tests and dev inspection.

The beacon carries client-stamped timestamps. We correct for clock skew using
a server_now exchange on SSE connect. Beacons with excessive skew or
suspicious ages are dropped.

Security: the POST endpoint is publicly reachable (clients may beacon before
auth resolves) but rate-limited per IP via a simple token bucket. Summary
endpoint stays internal/admin-only.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from pydantic import BaseModel
from pydantic import Field

from zerg.dependencies.auth import require_admin
from zerg.metrics import canary_latency_seconds
from zerg.metrics import canary_observations_total
from zerg.metrics import canary_seq_last_seen
from zerg.metrics import event_end_to_end_latency_seconds
from zerg.metrics import event_render_beacons_total

beacon_router = APIRouter(prefix="/telemetry", tags=["telemetry"])
admin_router = APIRouter(prefix="/telemetry", tags=["telemetry"], dependencies=[Depends(require_admin)])


# Simple per-IP token bucket: 20 beacons/sec, burst 60. This is plenty for
# a real user (one per event) but kills obvious flooding.
_BUCKET_CAPACITY = 60.0
_BUCKET_REFILL_PER_SEC = 20.0
_buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_refill_mono)
_buckets_max_size = 10_000  # Cap memory; evict LRU-ish by clearing when full.


def _take_token(ip: str, now: float) -> bool:
    tokens, last = _buckets.get(ip, (_BUCKET_CAPACITY, now))
    elapsed = max(0.0, now - last)
    tokens = min(_BUCKET_CAPACITY, tokens + elapsed * _BUCKET_REFILL_PER_SEC)
    if tokens < 1.0:
        _buckets[ip] = (tokens, now)
        return False
    tokens -= 1.0
    if len(_buckets) >= _buckets_max_size:
        _buckets.clear()
    _buckets[ip] = (tokens, now)
    return True


class RenderBeacon(BaseModel):
    """Single event-render beacon from a client."""

    event_id: str = Field(..., max_length=128)
    session_id: str | None = Field(None, max_length=128)
    surface: Literal["web", "ios"]
    managed: bool = False
    emitted_at_ms: int = Field(..., description="Server-stamped emitted_at for the event, in ms epoch")
    rendered_at_ms: int = Field(..., description="Client wall-clock render time, in ms epoch")
    clock_skew_ms: int = Field(0, description="Client-measured skew vs server (positive: client ahead)")


@dataclass
class _Sample:
    at_monotonic: float
    surface: str
    managed: bool
    latency_s: float


_samples: deque[_Sample] = deque(maxlen=2000)
_MAX_CLOCK_SKEW_MS = 30_000
_MAX_LATENCY_S = 60.0


@beacon_router.post("/client-render", include_in_schema=False)
async def client_render_beacon(beacons: list[RenderBeacon] | RenderBeacon, request: Request) -> dict:
    """Accept one or a batch of render beacons.

    Publicly reachable so clients can beacon before auth resolves, but
    rate-limited per source IP to defang obvious flooding.
    """
    now_mono = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    if not _take_token(client_ip, now_mono):
        event_render_beacons_total.labels(surface="web", outcome="rate_limited").inc()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many beacons")

    if isinstance(beacons, RenderBeacon):
        beacons = [beacons]

    accepted = 0
    dropped_skew = 0
    dropped_range = 0

    for b in beacons:
        # Drop samples with implausible client clocks — they poison percentiles.
        if abs(b.clock_skew_ms) > _MAX_CLOCK_SKEW_MS:
            dropped_skew += 1
            event_render_beacons_total.labels(surface=b.surface, outcome="skewed").inc()
            continue
        # rendered - emitted, corrected for skew (client_ahead -> subtract).
        latency_ms = (b.rendered_at_ms - b.clock_skew_ms) - b.emitted_at_ms
        if latency_ms < 0:
            # Clamp small negatives from sub-ms skew noise; drop large ones.
            if latency_ms > -500:
                latency_ms = 0
            else:
                dropped_range += 1
                event_render_beacons_total.labels(surface=b.surface, outcome="negative").inc()
                continue
        latency_s = latency_ms / 1000.0
        if latency_s > _MAX_LATENCY_S:
            dropped_range += 1
            event_render_beacons_total.labels(surface=b.surface, outcome="stale").inc()
            continue

        managed_label = "true" if b.managed else "false"
        event_end_to_end_latency_seconds.labels(surface=b.surface, managed=managed_label).observe(latency_s)
        event_render_beacons_total.labels(surface=b.surface, outcome="ok").inc()
        _samples.append(_Sample(now_mono, b.surface, b.managed, latency_s))
        accepted += 1

    return {"accepted": accepted, "dropped_skew": dropped_skew, "dropped_range": dropped_range}


@admin_router.get("/latency-summary", include_in_schema=False)
async def latency_summary(window_s: int = 900) -> dict:
    """Return p50/p95/p99 end-to-end latency over the last window_s seconds, grouped by surface+managed."""
    cutoff = time.monotonic() - max(30, min(window_s, 3600))
    groups: dict[tuple[str, bool], list[float]] = {}
    for s in _samples:
        if s.at_monotonic < cutoff:
            continue
        groups.setdefault((s.surface, s.managed), []).append(s.latency_s)

    def _pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sv = sorted(values)
        k = max(0, min(len(sv) - 1, int(round((p / 100.0) * (len(sv) - 1)))))
        return round(sv[k] * 1000, 1)

    summary = []
    for (surface, managed), values in sorted(groups.items()):
        summary.append(
            {
                "surface": surface,
                "managed": managed,
                "count": len(values),
                "p50_ms": _pct(values, 50),
                "p95_ms": _pct(values, 95),
                "p99_ms": _pct(values, 99),
            }
        )
    return {"window_s": window_s, "groups": summary}


# -----------------------------------------------------------------------------
# Canary observation endpoint
# -----------------------------------------------------------------------------


class CanaryObservation(BaseModel):
    """A single observation from a canary producer or consumer.

    hop identifies where in the pipeline the observation was taken:
      - "ingest": producer measured server receive vs its own emit
      - "sse":    SSE observer measured server wake vs producer emit
      - "render": browser/iOS measured rendered_at vs producer emit
    """

    canary_seq: int = Field(..., ge=0)
    hop: Literal["ingest", "sse", "render"]
    surface: str = Field("server", max_length=32)
    latency_ms: int = Field(..., ge=0, le=600_000)


_canary_last_obs_monotonic: dict[str, float] = {}


@admin_router.post("/canary-observation", include_in_schema=False)
async def canary_observation(obs: CanaryObservation) -> dict:
    """Record a canary latency observation.

    Admin-only because canary credentials control what gets counted; we do
    not want random clients polluting SLA signal.
    """
    latency_s = obs.latency_ms / 1000.0
    canary_latency_seconds.labels(hop=obs.hop, surface=obs.surface).observe(latency_s)
    canary_observations_total.labels(hop=obs.hop, outcome="ok").inc()
    canary_seq_last_seen.labels(hop=obs.hop).set(obs.canary_seq)
    _canary_last_obs_monotonic[obs.hop] = time.monotonic()
    return {"ok": True, "hop": obs.hop, "seq": obs.canary_seq}


def canary_last_obs_age_s(hop: str) -> float | None:
    """How long since we last saw a canary observation on this hop. None if never."""
    last = _canary_last_obs_monotonic.get(hop)
    if last is None:
        return None
    return round(time.monotonic() - last, 1)


# -----------------------------------------------------------------------------
# Admin selfcheck: surface config + canary health without a dashboard
# -----------------------------------------------------------------------------


_CANARY_HOPS = ("ingest", "sse", "render")


def _histogram_percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    sv = sorted(samples)
    k = max(0, min(len(sv) - 1, int(round((p / 100.0) * (len(sv) - 1)))))
    return round(sv[k] * 1000, 1)


@admin_router.get("/selfcheck", include_in_schema=False)
async def telemetry_selfcheck(window_s: int = 900) -> dict:
    """Surface canary health in one admin-visible GET.

    Ops pattern: a cron on the operator's laptop hits this and posts to a
    webhook on breach. No Alertmanager needed.

    Breach signals:
      - canary_<hop>_age_s > 120: pipeline hop is dead
      - canary_<hop>_p95_ms > target: SLA regression
      - seq_gap: observer fell behind producer (dropped events)
    """
    # NOTE: canary stats come from the per-hop last-obs-age tracker and the
    # prometheus Gauges, not the beacon deque. window_s is accepted for
    # symmetry with /latency-summary but only affects the caller's window
    # expectation — not how we read alive/seq.
    _ = window_s

    # `ingest` and `sse` are the load-bearing hops — the producer and
    # observer scripts must be running for the full pipeline to be
    # considered healthy. `render` is optional (only web/iOS beacons
    # populate it today) so a None there is not a breach signal.
    _REQUIRED_HOPS = {"ingest", "sse"}

    hops: dict[str, dict] = {}
    for hop in _CANARY_HOPS:
        age_s = canary_last_obs_age_s(hop)
        required = hop in _REQUIRED_HOPS
        alive = age_s is not None and age_s < 120.0
        hops[hop] = {
            "last_obs_age_s": age_s,
            "required": required,
            "alive": alive,
        }

    # Producer and observer must be roughly in lockstep; if one hop is far
    # ahead of the other we've lost events somewhere.
    # canary_seq_last_seen.labels(hop=...) is a Gauge; read its value.
    try:
        from zerg.metrics import canary_seq_last_seen as _gauge

        ingest_seq = _gauge.labels(hop="ingest")._value.get()  # type: ignore[attr-defined]
        sse_seq = _gauge.labels(hop="sse")._value.get()  # type: ignore[attr-defined]
        seq_gap = int(ingest_seq) - int(sse_seq)
    except Exception:
        ingest_seq = sse_seq = None
        seq_gap = None

    # Required hops must be alive; optional hops can be absent. Never-seen
    # required hops (age None) count as dead so selfcheck can't lie with
    # "ok" when the producer never started.
    required_ok = all(h["alive"] for h in hops.values() if h.get("required"))
    seq_ok = seq_gap is None or abs(seq_gap) < 10
    overall_ok = required_ok and seq_ok

    return {
        "ok": overall_ok,
        "window_s": window_s,
        "hops": hops,
        "seq": {
            "ingest": int(ingest_seq) if ingest_seq is not None else None,
            "sse": int(sse_seq) if sse_seq is not None else None,
            "gap": seq_gap,
        },
    }
