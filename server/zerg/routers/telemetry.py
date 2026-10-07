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

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from zerg.config import get_settings
from zerg.dependencies.agents_auth import owner_id_from_caller
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.dependencies.auth import require_admin
from zerg.dependencies.request_db import no_request_db
from zerg.metrics import canary_latency_seconds
from zerg.metrics import canary_observations_total
from zerg.metrics import canary_seq_last_seen
from zerg.metrics import event_end_to_end_latency_seconds
from zerg.metrics import event_render_beacons_total
from zerg.metrics import state_end_to_end_latency_seconds
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.services.session_observations import OBS_KIND_CLIENT_RENDER
from zerg.services.session_observations import SOURCE_DOMAIN_CLIENT
from zerg.services.session_observations import record_session_observation
from zerg.services.write_serializer import get_write_serializer


def canary_token_matches(request: Request) -> bool:
    """True if the request carries a valid X-Canary-Token."""
    settings = get_settings()
    header_token = request.headers.get("X-Canary-Token", "")
    if not settings.canary_token or not header_token:
        return False
    return hmac.compare_digest(header_token.encode("utf-8"), settings.canary_token.encode("utf-8"))


def require_canary_token(request: Request) -> None:
    """Gate the canary-only router: X-Canary-Token must match env."""
    if not canary_token_matches(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="canary token required")


beacon_router = APIRouter(prefix="/telemetry", tags=["telemetry"])
admin_router = APIRouter(prefix="/telemetry", tags=["telemetry"], dependencies=[Depends(require_admin)])
# Canary router is a separate auth surface from admin. Same endpoints are
# *also* exposed on admin_router so operators with a browser cookie still
# hit them; canary_router is the background-daemon path.
canary_router = APIRouter(prefix="/telemetry", tags=["telemetry"], dependencies=[Depends(require_canary_token)])


# Simple per-IP token bucket: 20 beacons/sec, burst 60. This is plenty for
# a real user (one per event) but kills obvious flooding.
_BUCKET_CAPACITY = 60.0
_BUCKET_REFILL_PER_SEC = 20.0
_buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_refill_mono)
_buckets_max_size = 10_000  # Cap memory; evict LRU-ish by clearing when full.
logger = logging.getLogger("longhouse.client_render")


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


class WebKitRenderDiagnostics(BaseModel):
    """Optional iOS WebKit transcript render diagnostics."""

    stage: Literal["queued", "rendered", "failed", "duplicate"]
    payload_byte_size: int = Field(..., ge=0, le=5_000_000)
    row_count: int = Field(..., ge=0, le=50_000)
    latest_item_id: str | None = Field(None, max_length=128)
    payload_fingerprint: str | None = Field(None, max_length=32)
    render_duration_ms: int | None = Field(None, ge=0, le=60_000)
    render_sequence: int = Field(..., ge=0, le=10_000_000)
    js_failure_count: int = Field(..., ge=0, le=10_000_000)
    should_stick_to_bottom: bool
    web_view_loaded: bool
    error_description: str | None = Field(None, max_length=512)


class RenderBeacon(BaseModel):
    """Single event or runtime-state render beacon from a client."""

    event_id: str = Field(..., max_length=128)
    session_id: str | None = Field(None, max_length=128)
    surface: Literal["web", "ios"]
    render_kind: Literal["event", "state"] = "event"
    managed: bool = False
    emitted_at_ms: int = Field(..., description="Server-stamped emitted_at for the event, in ms epoch")
    rendered_at_ms: int = Field(..., description="Client wall-clock render time, in ms epoch")
    clock_skew_ms: int = Field(0, description="Client-measured skew vs server (positive: client ahead)")
    server_fanout_at_ms: int | None = Field(None, description="Server fanout timestamp from the SSE frame, in ms epoch")
    client_received_at_ms: int | None = Field(
        None,
        description="Client wall-clock time when the SSE frame was received",
    )
    pubsub_seq: int | None = Field(None, description="Per-session pubsub sequence that woke the client")
    state_commit_seq: int | None = Field(None, ge=0, description="Canonical catalog commit rendered by the client")
    state_phase: str | None = Field(None, max_length=64, description="Canonical activity state rendered by the client")
    state_observed_at_ms: int | None = Field(None, description="Canonical activity observed_at rendered by the client")
    webkit: WebKitRenderDiagnostics | None = None


@dataclass
class _Sample:
    at_monotonic: float
    surface: str
    managed: bool
    latency_s: float
    session_id: str | None
    event_id: str


_samples: deque[_Sample] = deque(maxlen=2000)
_MAX_CLOCK_SKEW_MS = 30_000
_MAX_LATENCY_S = 60.0


def _utc_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _persist_render_beacon(db: Session, beacon: RenderBeacon, *, latency_ms: int) -> None:
    if not beacon.session_id:
        return
    try:
        session_id = UUID(str(beacon.session_id))
    except ValueError:
        return

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    provider = session.provider if session else "unknown"
    observed_at = _utc_from_ms(beacon.rendered_at_ms - beacon.clock_skew_ms)
    payload = {
        "event_id": beacon.event_id,
        "surface": beacon.surface,
        "managed": beacon.managed,
        "emitted_at_ms": beacon.emitted_at_ms,
        "rendered_at_ms": beacon.rendered_at_ms,
        "clock_skew_ms": beacon.clock_skew_ms,
        "server_fanout_at_ms": beacon.server_fanout_at_ms,
        "client_received_at_ms": beacon.client_received_at_ms,
        "pubsub_seq": beacon.pubsub_seq,
        "render_kind": beacon.render_kind,
        "state_commit_seq": beacon.state_commit_seq,
        "state_phase": beacon.state_phase,
        "state_observed_at_ms": beacon.state_observed_at_ms,
        "latency_ms": latency_ms,
    }
    if beacon.webkit is not None:
        payload["webkit"] = beacon.webkit.model_dump(exclude_none=True)
    record_session_observation(
        db,
        observation_id=(f"client_render:{beacon.surface}:{session_id}:{beacon.event_id}:{beacon.rendered_at_ms}"),
        session_id=session_id,
        runtime_key=None,
        provider=provider,
        device_id=session.device_id if session else None,
        source_domain=SOURCE_DOMAIN_CLIENT,
        source="client_render_beacon",
        kind=OBS_KIND_CLIENT_RENDER,
        source_cursor=f"event:{beacon.event_id}",
        observed_at=observed_at,
        payload=payload,
    )


async def _persist_render_beacons(
    db: Session,
    beacons: list[tuple[RenderBeacon, int]],
) -> None:
    if not beacons:
        return

    def _do(write_db: Session) -> None:
        for beacon, latency_ms in beacons:
            _persist_render_beacon(write_db, beacon, latency_ms=latency_ms)

    await get_write_serializer().execute_or_direct(_do, db, label="client-render")


@beacon_router.post("/client-render", include_in_schema=False)
async def client_render_beacon(
    beacons: list[RenderBeacon] | RenderBeacon,
    request: Request,
    db: Session | None = Depends(no_request_db),
) -> dict:
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
    persistable: list[tuple[RenderBeacon, int]] = []

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
            if b.webkit is not None:
                # Historical session reads can render minutes or hours after
                # the underlying event was emitted. Keep those WebKit facts for
                # forensics, but do not put stale samples into latency metrics.
                persistable.append((b, int(round(latency_ms))))
                accepted += 1
            continue

        webkit_stage = b.webkit.stage if b.webkit else None
        if webkit_stage == "failed":
            event_render_beacons_total.labels(surface=b.surface, outcome="render_failed").inc()
        else:
            managed_label = "true" if b.managed else "false"
            if b.render_kind == "state":
                state_end_to_end_latency_seconds.labels(surface=b.surface, managed=managed_label).observe(latency_s)
            else:
                event_end_to_end_latency_seconds.labels(surface=b.surface, managed=managed_label).observe(latency_s)
            event_render_beacons_total.labels(surface=b.surface, outcome="ok").inc()
            if b.render_kind == "event":
                _samples.append(_Sample(now_mono, b.surface, b.managed, latency_s, b.session_id, b.event_id))
        rounded_latency_ms = int(round(latency_ms))
        persistable.append((b, rounded_latency_ms))
        logger.info(
            "client_render session=%s event=%s surface=%s latency_ms=%d pubsub_seq=%s",
            b.session_id,
            b.event_id,
            b.surface,
            rounded_latency_ms,
            b.pubsub_seq,
        )
        accepted += 1

    if db is not None:
        try:
            await _persist_render_beacons(db, persistable)
        except Exception:
            # Metrics and structured logs must not depend on forensic persistence.
            logger.exception("client_render persistence failed")

    return {"accepted": accepted, "dropped_skew": dropped_skew, "dropped_range": dropped_range}


@admin_router.get("/client-render/recent", include_in_schema=False)
async def recent_client_render_beacons(
    session_id: str | None = None,
    event_id: str | None = None,
    limit: int = 50,
    db: Session | None = Depends(no_request_db),
) -> dict:
    """Return recent persisted browser/iOS render beacons for forensic debugging."""
    if db is None:
        return {"items": [], "persistence": "structured_logs"}
    query = (
        db.query(SessionObservation)
        .filter(SessionObservation.source_domain == SOURCE_DOMAIN_CLIENT)
        .filter(SessionObservation.kind == OBS_KIND_CLIENT_RENDER)
    )
    if session_id:
        try:
            query = query.filter(SessionObservation.session_id == UUID(str(session_id)))
        except ValueError:
            return {"items": []}
    if event_id:
        query = query.filter(SessionObservation.source_cursor == f"event:{event_id}")

    rows = query.order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc()).limit(max(1, min(limit, 200))).all()
    items = []
    for row in rows:
        import json

        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        items.append(
            {
                "session_id": str(row.session_id) if row.session_id else None,
                "event_id": payload.get("event_id"),
                "surface": payload.get("surface"),
                "managed": payload.get("managed"),
                "latency_ms": payload.get("latency_ms"),
                "emitted_at_ms": payload.get("emitted_at_ms"),
                "rendered_at_ms": payload.get("rendered_at_ms"),
                "clock_skew_ms": payload.get("clock_skew_ms"),
                "server_fanout_at_ms": payload.get("server_fanout_at_ms"),
                "client_received_at_ms": payload.get("client_received_at_ms"),
                "pubsub_seq": payload.get("pubsub_seq"),
                "render_kind": payload.get("render_kind", "event"),
                "state_commit_seq": payload.get("state_commit_seq"),
                "state_phase": payload.get("state_phase"),
                "state_observed_at_ms": payload.get("state_observed_at_ms"),
                "webkit": payload.get("webkit"),
                "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                "received_at": row.received_at.isoformat() if row.received_at else None,
            }
        )
    return {"items": items}


class ClientDiagnosticEntry(BaseModel):
    """One client-side lifecycle mark: what the app did, when, for which session."""

    at_ms: int = Field(..., description="Client wall-clock time of the mark, in ms epoch")
    stage: str = Field(..., max_length=64)
    detail: str | None = Field(None, max_length=512)
    session_id: str | None = Field(None, max_length=128)


class ClientDiagnosticsBatch(BaseModel):
    """A batch of client lifecycle marks (stream connects, stalls, polls, reconciles).

    The phone cannot be tailed like a server, so the app ships the marks it
    already logs locally. They land as one log line each so the operator can
    read the client's view of a session next to the server's.
    """

    surface: Literal["web", "ios"]
    device_label: str | None = Field(None, max_length=64)
    app_build: str | None = Field(None, max_length=64)
    entries: list[ClientDiagnosticEntry] = Field(..., max_length=200)


diag_logger = logging.getLogger("longhouse.client_diag")


@beacon_router.post("/client-diagnostics", include_in_schema=False)
async def client_diagnostics(batch: ClientDiagnosticsBatch, request: Request) -> dict:
    """Accept a batch of client lifecycle marks. Same public/rate-limited posture as render beacons.

    The log line is the whole sink: the operator tool greps the tenant log,
    and a second in-process copy would die with the worker anyway.
    """
    now_mono = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    if not _take_token(client_ip, now_mono):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many beacons")
    for entry in batch.entries:
        diag_logger.info(
            "client_diag surface=%s device=%s build=%s session=%s at=%s stage=%s %s",
            batch.surface,
            batch.device_label,
            batch.app_build,
            entry.session_id,
            datetime.fromtimestamp(entry.at_ms / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds"),
            entry.stage,
            entry.detail or "",
        )
    return {"accepted": len(batch.entries)}


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
      - "ingest": producer's committed-acknowledgement round-trip latency
      - "sse":    observer receipt time minus producer emission
      - "render": browser/iOS rendered_at minus producer emission
    """

    canary_seq: int = Field(..., ge=0)
    hop: Literal["ingest", "sse", "render"]
    surface: str = Field("server", max_length=32)
    latency_ms: int = Field(..., ge=0, le=600_000)


_canary_last_obs_monotonic: dict[str, float] = {}
_CANARY_LATENCY_WINDOW_S = 900.0
_CANARY_LATENCY_SAMPLE_LIMIT = 1024
_canary_latency_samples: deque[tuple[float, str, str, float]] = deque(maxlen=_CANARY_LATENCY_SAMPLE_LIMIT)


def record_canary_observation(*, canary_seq: int, hop: str, surface: str, latency_ms: int) -> None:
    latency_s = latency_ms / 1000.0
    now_monotonic = time.monotonic()
    canary_latency_seconds.labels(hop=hop, surface=surface).observe(latency_s)
    canary_observations_total.labels(hop=hop, outcome="ok").inc()
    canary_seq_last_seen.labels(hop=hop).set(canary_seq)
    _canary_last_obs_monotonic[hop] = now_monotonic
    _canary_latency_samples.append((now_monotonic, hop, surface, latency_s))


@canary_router.get("/canary-session", include_in_schema=False)
async def canary_session_lookup() -> dict:
    """Return the session_id of the currently-live canary producer.

    "Live" means the session has seen runtime activity within the last 5 minutes.
    This excludes abandoned stress-run sessions that happened to have a
    newer last_activity_at than the always-on producer. Used by the
    Playwright render-canary test in CI to discover the target session
    without plumbing a UUID through env. Gated by canary token.
    """
    from datetime import datetime
    from datetime import timezone

    from zerg.catalogd.client import CatalogRemoteError
    from zerg.catalogd.client import CatalogUnavailable
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "catalog_unavailable",
                "message": "Canary session lookup requires a live catalog.",
            },
        )
    observed_at = datetime.now(timezone.utc)
    try:
        result = await catalogd.call(
            "storage.session.canary.lookup.v2",
            {
                "observed_at": observed_at.isoformat(),
                "max_age_seconds": 300,
            },
        )
    except (CatalogUnavailable, CatalogRemoteError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "catalog_unavailable",
                "message": "Canary session lookup catalog is unavailable.",
            },
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "catalog_unavailable",
                "message": "Canary session lookup returned an invalid catalog payload.",
            },
        )
    session_id = result.get("session_id")
    if session_id is None:
        return {"session_id": None}
    try:
        canonical_session_id = str(UUID(session_id)) if isinstance(session_id, str) else None
    except ValueError:
        canonical_session_id = None
    if canonical_session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "catalog_unavailable",
                "message": "Canary session lookup returned an invalid session_id.",
            },
        )
    return {"session_id": canonical_session_id}


@canary_router.post("/canary-observation", include_in_schema=False)
async def canary_observation(obs: CanaryObservation) -> dict:
    """Record a canary latency observation.

    Gated by X-Canary-Token (shared secret env var). Keeps random clients
    from polluting SLA signal without requiring a browser cookie — the
    producer + observer run without a browser cookie on the build host.
    """
    record_canary_observation(
        canary_seq=obs.canary_seq,
        hop=obs.hop,
        surface=obs.surface,
        latency_ms=obs.latency_ms,
    )
    return {"ok": True, "hop": obs.hop, "seq": obs.canary_seq}


def canary_last_obs_age_s(hop: str) -> float | None:
    """How long since we last saw a canary observation on this hop. None if never."""
    last = _canary_last_obs_monotonic.get(hop)
    if last is None:
        return None
    return round(time.monotonic() - last, 1)


# -----------------------------------------------------------------------------
# Authenticated canary selfcheck: hop health and bounded SLA summary
# -----------------------------------------------------------------------------


_CANARY_HOPS = ("ingest", "sse", "render")
_CANARY_SSE_P95_TARGET_MS = 300.0


def _histogram_percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    sv = sorted(samples)
    k = max(0, min(len(sv) - 1, int(round((p / 100.0) * (len(sv) - 1)))))
    return round(sv[k] * 1000, 1)


@canary_router.get("/selfcheck", include_in_schema=False)
async def telemetry_selfcheck(window_s: int = 900) -> dict:
    """Report required-hop liveness, sequence correlation, and recent SSE SLA.

    Sauron polls this canary-token-protected endpoint. It returns only the
    bounded p95/sample-count summary used by the realtime canary watchdog.
    """
    # The latency summary below is a bounded recent view of observations that
    # were submitted through the authenticated canary observation route.
    window_s = max(30, min(int(window_s), int(_CANARY_LATENCY_WINDOW_S)))

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
    seq_ok = seq_gap is not None and abs(seq_gap) < 10
    sample_cutoff = time.monotonic() - window_s
    sse_samples = [
        latency_s
        for observed_at, hop, surface, latency_s in _canary_latency_samples
        if observed_at >= sample_cutoff and hop == "sse" and surface == "observer"
    ]
    sse_p95_ms = _histogram_percentile(sse_samples, 95) if sse_samples else None
    latency_ok = sse_p95_ms is not None and sse_p95_ms <= _CANARY_SSE_P95_TARGET_MS
    overall_ok = required_ok and seq_ok and latency_ok

    return {
        "ok": overall_ok,
        "window_s": window_s,
        "hops": hops,
        "seq": {
            "ingest": int(ingest_seq) if ingest_seq is not None else None,
            "sse": int(sse_seq) if sse_seq is not None else None,
            "gap": seq_gap,
        },
        "metrics": {
            "window_s": window_s,
            "sse_sample_count": len(sse_samples),
            "sse_p95_ms": sse_p95_ms,
        },
    }


async def _canary_workspace_stream(request: Request, *, session_id: UUID, owner_id: int):
    """Project only live producer markers from the canonical workspace invalidation stream."""
    from zerg.routers.timeline import _live_catalog_workspace_stream

    async for frame in _live_catalog_workspace_stream(
        request,
        session_id=session_id,
        skip_initial=False,
        owner_id=owner_id,
        last_event_id=None,
        stream_epoch=None,
        include_canary_markers=True,
        heartbeat_interval_seconds=5.0,
    ):
        event_name = frame.get("event")
        try:
            data = json.loads(frame.get("data") or "{}")
        except (TypeError, json.JSONDecodeError):
            yield {"event": "error", "data": json.dumps({"error": "invalid_workspace_event"})}
            return
        if not isinstance(data, dict):
            yield {"event": "error", "data": json.dumps({"error": "invalid_workspace_event"})}
            return
        if event_name == "connected":
            server_now_ms = data.get("server_now_ms")
            if type(server_now_ms) is int:
                yield {"event": "connected", "data": json.dumps({"server_now_ms": server_now_ms})}
            continue
        if event_name == "heartbeat":
            timestamp = data.get("timestamp")
            if isinstance(timestamp, str):
                yield {"event": "heartbeat", "data": json.dumps({"timestamp": timestamp})}
            continue
        if event_name == "error":
            error = data.get("error")
            yield {
                "event": "error",
                "data": json.dumps({"error": error if error == "session_not_found" else "workspace_unavailable"}),
            }
            return
        if event_name != "workspace_changed":
            continue
        if "canary_seq" not in data and "canary_emitted_at_ms" not in data:
            # Initial invalidations and unrelated session updates are not
            # producer deliveries and must not refresh the canary.
            continue
        canary_seq = data.get("canary_seq")
        emitted_at_ms = data.get("canary_emitted_at_ms")
        server_fanout_at_ms = data.get("server_fanout_at_ms")
        server_now_ms = data.get("server_now_ms")
        pubsub_seq = data.get("pubsub_seq")
        if (
            type(canary_seq) is not int
            or canary_seq < 0
            or type(emitted_at_ms) is not int
            or emitted_at_ms <= 0
            or type(server_fanout_at_ms) is not int
            or server_fanout_at_ms <= 0
            or type(server_now_ms) is not int
            or server_now_ms <= 0
            or type(pubsub_seq) is not int
            or pubsub_seq <= 0
        ):
            yield {"event": "error", "data": json.dumps({"error": "invalid_canary_marker"})}
            return
        yield {
            "event": "canary_observation",
            "id": frame.get("id"),
            "data": json.dumps(
                {
                    "canary_seq": canary_seq,
                    "canary_emitted_at_ms": emitted_at_ms,
                    "server_fanout_at_ms": server_fanout_at_ms,
                    "server_now_ms": server_now_ms,
                    "pubsub_seq": pubsub_seq,
                }
            ),
        }


@canary_router.get("/canary-stream", include_in_schema=False)
async def canary_workspace_stream(
    request: Request,
    session_id: UUID,
    auth: object = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> EventSourceResponse:
    """Stream live canary markers after owner-scoped canonical session lookup."""
    from zerg.services.catalog_read_gateway import CatalogReadError
    from zerg.services.live_catalog_timeline import read_live_catalog_session

    owner_id = owner_id_from_caller(auth)
    try:
        session, _provider_alias, _commit_seq = await asyncio.to_thread(
            read_live_catalog_session,
            session_id,
            owner_id=owner_id,
        )
    except CatalogReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if session is None or str(session.provider or "").strip().lower() != "canary":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canary session not found")
    return EventSourceResponse(_canary_workspace_stream(request, session_id=session_id, owner_id=owner_id))
