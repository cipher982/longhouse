"""Admin-only Ops Dashboard APIs."""

from __future__ import annotations

import time
from collections import defaultdict
from collections import deque
from datetime import datetime
from datetime import timezone
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import require_admin
from zerg.models.models import User as UserModel
from zerg.schemas.ops import OpsSummary
from zerg.schemas.ops import TimeSeriesResponse
from zerg.schemas.ops import TopAutomationsResponse
from zerg.services.ops_service import get_summary as svc_get_summary
from zerg.services.ops_service import get_timeseries as svc_get_timeseries
from zerg.services.ops_service import get_top_automations as svc_get_top_automations

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_admin)])

# ---------------------------------------------------------------------------
# Frontend Error Beacon (public, no auth required)
# ---------------------------------------------------------------------------
beacon_router = APIRouter(prefix="/ops", tags=["ops"])
_frontend_errors: list[dict[str, Any]] = []


# Cap the unauthenticated beacon body so it can't be used to push large
# payloads into the in-memory ring buffer, and rate-limit per IP so it can't be
# used to burn CPU/event-loop with a flood of small beacons.
_BEACON_MAX_BODY_BYTES = 16 * 1024
_BEACON_RATE_WINDOW_SECONDS = 60.0
_BEACON_RATE_MAX = 30  # per IP per window
_beacon_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _beacon_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    window_start = now - _BEACON_RATE_WINDOW_SECONDS
    # Bound memory: when the keyspace grows large, drop OTHER buckets whose
    # window has fully expired. Never drop the current ip's bucket here (that
    # would reset its count and let it flood).
    if len(_beacon_rate_buckets) > 4096:
        for key in [k for k, b in _beacon_rate_buckets.items() if k != ip and (not b or b[-1] < window_start)]:
            del _beacon_rate_buckets[key]
    bucket = _beacon_rate_buckets[ip]
    while bucket and bucket[0] < window_start:
        bucket.popleft()
    if len(bucket) >= _BEACON_RATE_MAX:
        return True
    bucket.append(now)
    return False


async def _read_capped_body(request: Request, limit: int) -> bytes | None:
    """Read the request body incrementally, aborting once it exceeds ``limit``.

    Avoids buffering an arbitrarily large body: bails on the Content-Length hint
    and stops consuming the stream as soon as the cap is crossed.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@beacon_router.post("/beacon", include_in_schema=False)
async def error_beacon(request: Request):
    """Capture frontend errors from anonymous users. No auth required."""
    try:
        ip = request.client.host if request.client else "unknown"
        if _beacon_rate_limited(ip):
            return {}
        raw = await _read_capped_body(request, _BEACON_MAX_BODY_BYTES)
        if raw is None:
            # Too large: ignore silently (never fail the beacon).
            return {}
        import json

        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        data["ts"] = datetime.now(timezone.utc).isoformat()
        data["ip"] = ip
        _frontend_errors.append(data)
        if len(_frontend_errors) > 500:
            _frontend_errors.pop(0)
    except Exception:
        pass  # Never fail the beacon
    return {}


@router.get("/errors")
def get_frontend_errors(current_user: UserModel = Depends(require_admin)):
    """Admin-only: view recent frontend errors captured via beacon."""
    return _frontend_errors[-100:]


@router.get("/summary", response_model=OpsSummary)
def get_summary(
    window: str = Query("today", pattern="^(today|7d|30d)$"),
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return primary KPIs for the Ops dashboard (admin-only)."""
    return svc_get_summary(db, current_user, window=window)


@router.get("/timeseries", response_model=TimeSeriesResponse)
def get_timeseries(
    metric: str = Query(
        ...,
        pattern="^(runs_by_hour|errors_by_hour|cost_by_hour|runs_by_day|errors_by_day|cost_by_day)$",
    ),
    window: str = Query("today", pattern="^(today|7d|30d)$"),
    db: Session = Depends(get_db),
):
    try:
        series_data = svc_get_timeseries(db, metric=metric, window=window)
        return TimeSeriesResponse(series=series_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/top", response_model=TopAutomationsResponse)
def get_top(
    kind: str = Query("automations", pattern="^automations$"),
    window: str = Query("today", pattern="^(today|7d|30d)$"),
    limit: int = 5,
    db: Session = Depends(get_db),
):
    if kind != "automations":
        raise HTTPException(status_code=400, detail="Only kind=automations supported")
    try:
        top_automations = svc_get_top_automations(db, window=window, limit=limit)
        return TopAutomationsResponse(top_automations=top_automations)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
