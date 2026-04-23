"""Cross-session managed turn observability endpoints."""

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
from zerg.services.session_turns import list_slow_session_turns
from zerg.services.session_views import SessionTurnTimingResponse
from zerg.services.session_views import build_session_turn_timing_response
from zerg.utils.time import UTCBaseModel

router = APIRouter(prefix="/agents/turns", tags=["agents"])

MachineHealthStatus = Literal["healthy", "degraded", "offline", "broken"]


class SlowTurnMachineResponse(UTCBaseModel):
    device_id: str
    status: MachineHealthStatus
    status_reason: str
    status_summary: str
    last_heartbeat_at: datetime
    heartbeat_age_seconds: int
    is_stale: bool
    version: str | None = None


class SlowTurnItemResponse(UTCBaseModel):
    turn_id: int
    session_id: str
    request_id: str | None = None
    provider: str
    project: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    managed_transport: str | None = None
    state: str
    terminal_phase: str | None = None
    error_code: str | None = None
    user_submitted_at: datetime
    completed_at: datetime
    total_turn_time_ms: int
    timing: SessionTurnTimingResponse
    machine: SlowTurnMachineResponse | None = None


class SlowTurnsListResponse(UTCBaseModel):
    turns: list[SlowTurnItemResponse]
    total: int
    hours_back: int
    min_total_turn_time_ms: int


@router.get("/slow", response_model=SlowTurnsListResponse)
async def list_slow_turns(
    provider: str | None = Query(None, description="Filter by session provider"),
    project: str | None = Query(None, description="Filter by project"),
    device_id: str | None = Query(None, description="Filter by device"),
    state: str | None = Query(None, description="Filter by turn state"),
    machine_status: MachineHealthStatus | None = Query(None, description="Filter by current machine transport state"),
    min_total_turn_time_ms: int = Query(
        30_000,
        ge=1_000,
        le=60 * 60 * 1_000,
        description="Only return completed turns at or above this total duration",
    ),
    hours_back: int = Query(
        24,
        ge=1,
        le=24 * 7,
        description="Only consider turns submitted within this recent window",
    ),
    limit: int = Query(20, ge=1, le=100, description="Max slow-turn rows to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    stale_after_seconds: int = Query(
        DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
        ge=60,
        le=24 * 60 * 60,
        description="Treat heartbeats older than this as offline when enriching machine status",
    ),
    db: Session = Depends(get_db),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SlowTurnsListResponse:
    """List recent slow managed turns across sessions, enriched with current machine health."""

    summaries, total = list_slow_session_turns(
        db,
        provider=provider,
        project=project,
        device_id=device_id,
        state=state,
        machine_status=machine_status,
        min_total_turn_time_ms=min_total_turn_time_ms,
        hours_back=hours_back,
        stale_after_seconds=stale_after_seconds,
        limit=limit,
        offset=offset,
    )
    return SlowTurnsListResponse(
        turns=[
            SlowTurnItemResponse(
                turn_id=int(item.turn.id),
                session_id=str(item.session.id),
                request_id=item.turn.request_id,
                provider=item.session.provider,
                project=item.session.project,
                device_id=item.session.device_id,
                device_name=item.session.device_name,
                managed_transport=item.session.managed_transport,
                state=item.turn.state,
                terminal_phase=item.turn.terminal_phase,
                error_code=item.turn.error_code,
                user_submitted_at=item.turn.user_submitted_at,
                completed_at=item.completed_at,
                total_turn_time_ms=item.total_turn_time_ms,
                timing=build_session_turn_timing_response(item.turn),
                machine=(
                    SlowTurnMachineResponse(
                        device_id=item.machine.device_id,
                        status=item.machine.status,  # type: ignore[arg-type]
                        status_reason=item.machine.status_reason,
                        status_summary=item.machine.status_summary,
                        last_heartbeat_at=item.machine.last_heartbeat_at,
                        heartbeat_age_seconds=item.machine.heartbeat_age_seconds,
                        is_stale=item.machine.is_stale,
                        version=item.machine.version,
                    )
                    if item.machine is not None
                    else None
                ),
            )
            for item in summaries
        ],
        total=total,
        hours_back=hours_back,
        min_total_turn_time_ms=min_total_turn_time_ms,
    )
