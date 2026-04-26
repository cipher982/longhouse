"""Client-side realtime latency telemetry.

Accepts render beacons from web/iOS after they display an event, and exposes
a small summary endpoint for quick SLA tracking without a TSDB scrape.

The beacon carries client-stamped timestamps. We correct for clock skew using
a server_now exchange on SSE connect (see sse routers). Beacons with excessive
skew or suspicious ages are dropped.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from zerg.metrics import event_end_to_end_latency_seconds
from zerg.metrics import event_render_beacons_total

beacon_router = APIRouter(prefix="/telemetry", tags=["telemetry"])
admin_router = APIRouter(prefix="/telemetry", tags=["telemetry"])


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
    """Accept one or a batch of render beacons. Public, no auth: clients post as they render."""
    if isinstance(beacons, RenderBeacon):
        beacons = [beacons]

    now_mono = time.monotonic()
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
