"""Machine-facing health summaries built from latest heartbeat rows."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import list_machine_transport_health
from zerg.utils.time import UTCBaseModel

router = APIRouter(prefix="/agents/machines", tags=["agents"])

MachineHealthStatus = Literal["healthy", "degraded", "offline", "broken"]


class MachineHealthItemResponse(UTCBaseModel):
    device_id: str
    version: str | None = None
    last_heartbeat_at: datetime
    heartbeat_age_seconds: int
    stale_after_seconds: int
    is_stale: bool
    status: MachineHealthStatus
    status_reason: str
    status_summary: str
    reasons: list[str]
    last_ship_at: datetime | None = None
    last_ship_attempt_at: datetime | None = None
    last_ship_result: str | None = None
    last_ship_latency_ms: int | None = None
    last_ship_http_status: int | None = None
    ship_attempts_1h: int
    ship_successes_1h: int
    ship_success_rate_1h: float | None = None
    ship_rate_limited_1h: int
    ship_server_errors_1h: int
    ship_payload_rejections_1h: int
    ship_payload_too_large_1h: int
    ship_retryable_client_errors_1h: int
    ship_connect_errors_1h: int
    ship_latency_p50_ms_1h: int | None = None
    ship_latency_p95_ms_1h: int | None = None
    spool_pending: int
    spool_dead: int
    parse_errors_1h: int
    consecutive_failures: int
    disk_free_bytes: int
    is_offline: bool


class MachineHealthListResponse(UTCBaseModel):
    machines: list[MachineHealthItemResponse]
    total: int


@router.get("/health", response_model=MachineHealthListResponse)
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
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineHealthListResponse:
    summaries, total = list_machine_transport_health(
        db,
        device_id=device_id,
        status=status,
        stale_after_seconds=stale_after_seconds,
        limit=limit,
    )
    return MachineHealthListResponse(
        machines=[
            MachineHealthItemResponse(
                device_id=item.device_id,
                version=item.version,
                last_heartbeat_at=item.last_heartbeat_at,
                heartbeat_age_seconds=item.heartbeat_age_seconds,
                stale_after_seconds=item.stale_after_seconds,
                is_stale=item.is_stale,
                status=item.status,  # type: ignore[arg-type]
                status_reason=item.status_reason,
                status_summary=item.status_summary,
                reasons=list(item.reasons),
                last_ship_at=item.last_ship_at,
                last_ship_attempt_at=item.last_ship_attempt_at,
                last_ship_result=item.last_ship_result,
                last_ship_latency_ms=item.last_ship_latency_ms,
                last_ship_http_status=item.last_ship_http_status,
                ship_attempts_1h=item.ship_attempts_1h,
                ship_successes_1h=item.ship_successes_1h,
                ship_success_rate_1h=item.ship_success_rate_1h,
                ship_rate_limited_1h=item.ship_rate_limited_1h,
                ship_server_errors_1h=item.ship_server_errors_1h,
                ship_payload_rejections_1h=item.ship_payload_rejections_1h,
                ship_payload_too_large_1h=item.ship_payload_too_large_1h,
                ship_retryable_client_errors_1h=item.ship_retryable_client_errors_1h,
                ship_connect_errors_1h=item.ship_connect_errors_1h,
                ship_latency_p50_ms_1h=item.ship_latency_p50_ms_1h,
                ship_latency_p95_ms_1h=item.ship_latency_p95_ms_1h,
                spool_pending=item.spool_pending,
                spool_dead=item.spool_dead,
                parse_errors_1h=item.parse_errors_1h,
                consecutive_failures=item.consecutive_failures,
                disk_free_bytes=item.disk_free_bytes,
                is_offline=item.is_offline,
            )
            for item in summaries
        ],
        total=total,
    )
