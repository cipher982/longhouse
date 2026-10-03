"""Browser-facing observability routes over the canonical machine telemetry."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from zerg.auth.caller import Caller
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_auth import get_current_browser_caller
from zerg.dependencies.request_db import no_request_db
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatus
from zerg.schemas.observability import ProductHealthCheckSummaryResponse
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEALTH_RECENT_WITHIN_SECONDS
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import machine_transport_health_from_catalog_rows
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import machine_heartbeats
from zerg.services.observability_views import build_machine_health_list_response
from zerg.services.product_health import build_session_title_health_check
from zerg.utils.time import utc_now

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_browser_caller), Depends(require_single_tenant)],
)

agents_router = APIRouter(
    prefix="/agents/observability",
    tags=["agents"],
    dependencies=[Depends(verify_agents_token), Depends(require_single_tenant)],
)


def _resolve_recent_machine_window_seconds(*, recent_within_hours: int) -> int:
    return max(1, recent_within_hours) * 60 * 60


@agents_router.get("/checks/session_titles", response_model=ProductHealthCheckSummaryResponse)
async def read_agent_session_title_health_check(
    window: str = Query("15m", description="Recent observation window such as 15m, 1h, or 7d"),
) -> ProductHealthCheckSummaryResponse:
    """Dependency-only product health with no retired archive-store reads."""

    try:
        return build_session_title_health_check(window=window)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/machines/health", response_model=MachineHealthListResponse)
async def list_machine_health(
    device_id: str | None = Query(None, description="Filter to one device"),
    status: MachineHealthStatus | None = Query(None, description="Filter by derived machine transport state"),
    limit: int = Query(20, ge=1, le=100, description="Max machine rows to return"),
    stale_after_seconds: int = Query(
        DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
        ge=60,
        le=24 * 60 * 60,
        description="Treat heartbeats older than this as offline",
    ),
    recent_within_hours: int = Query(
        DEFAULT_MACHINE_HEALTH_RECENT_WITHIN_SECONDS // 3600,
        ge=1,
        le=24 * 30,
        description="Only include machines with a heartbeat in this recent window",
    ),
    db: Session | None = Depends(no_request_db),
    caller: Caller = Depends(get_current_browser_caller),
) -> MachineHealthListResponse:
    recent_within_seconds = _resolve_recent_machine_window_seconds(
        recent_within_hours=recent_within_hours,
    )
    try:
        payload = machine_heartbeats(
            owner_id=caller.owner_id,
            device_id=device_id,
            recent_after=(utc_now() - timedelta(seconds=recent_within_seconds)).isoformat(),
            limit=100,
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    summaries, total = machine_transport_health_from_catalog_rows(
        payload.get("heartbeats", []),
        status=status,
        stale_after_seconds=stale_after_seconds,
        limit=limit,
    )
    return build_machine_health_list_response(summaries, total=total)
