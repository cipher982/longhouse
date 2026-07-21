"""Browser-facing observability routes over the canonical machine telemetry."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.auth import get_current_user
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.schemas.observability import MachineHealthListResponse
from zerg.schemas.observability import MachineHealthStatus
from zerg.schemas.observability import ManagedTurnsSummaryEnvelopeResponse
from zerg.schemas.observability import ObservabilityOverviewResponse
from zerg.schemas.observability import ProductHealthCheckListResponse
from zerg.schemas.observability import ProductHealthCheckLivePreviewResponse
from zerg.schemas.observability import RealtimePropagationSessionReportResponse
from zerg.schemas.observability import SlowTurnsListResponse
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEALTH_RECENT_WITHIN_SECONDS
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import list_machine_transport_health
from zerg.services.observability_views import build_machine_health_list_response
from zerg.services.observability_views import build_managed_turns_summary_envelope_response
from zerg.services.observability_views import build_observability_overview_response
from zerg.services.observability_views import build_slow_turns_list_response
from zerg.services.product_health import build_live_preview_check
from zerg.services.product_health import build_product_health_checks
from zerg.services.realtime_propagation import build_realtime_propagation_session_report
from zerg.services.session_turns import list_managed_completed_turns
from zerg.services.session_turns import list_slow_session_turns
from zerg.services.session_turns import materialize_recent_managed_transcript_turns

router = APIRouter(
    prefix="/observability",
    tags=["observability"],
    dependencies=[Depends(get_current_user), Depends(require_single_tenant)],
)


def _live_machine_health_db():
    factory = database_module.get_live_session_factory()
    if factory is None:
        raise HTTPException(status_code=503, detail={"code": "live_store_unavailable", "message": "Live telemetry is unavailable."})
    with factory() as db:
        yield db


_machine_health_db_dependency = _live_machine_health_db if database_module.live_catalog_enabled() else get_db
_machine_health_model = LiveHeartbeatStamp if database_module.live_catalog_enabled() else None


def _resolve_recent_machine_window_seconds(*, recent_within_hours: int) -> int:
    return max(1, recent_within_hours) * 60 * 60


@router.get("/checks", response_model=ProductHealthCheckListResponse)
async def list_product_health_checks(
    window: str = Query("15m", description="Recent observation window such as 15m, 1h, or 7d"),
    provider: str | None = Query(None, description="Filter live-preview observations by provider"),
    surface: str | None = Query(None, description="Filter live-preview observations by client surface"),
    managed: bool | None = Query(None, description="Filter live-preview observations by managed-session flag"),
    db: Session = Depends(get_db),
) -> ProductHealthCheckListResponse:
    try:
        return build_product_health_checks(
            db,
            window=window,
            provider=provider,
            surface=surface,
            managed=managed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/checks/live_preview", response_model=ProductHealthCheckLivePreviewResponse)
async def read_live_preview_health_check(
    window: str = Query("15m", description="Recent observation window such as 15m, 1h, or 7d"),
    provider: str | None = Query(None, description="Filter observations by provider"),
    surface: str | None = Query(None, description="Filter observations by client surface"),
    managed: bool | None = Query(None, description="Filter observations by managed-session flag"),
    db: Session = Depends(get_db),
) -> ProductHealthCheckLivePreviewResponse:
    try:
        return build_live_preview_check(
            db,
            window=window,
            provider=provider,
            surface=surface,
            managed=managed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/latency", response_model=RealtimePropagationSessionReportResponse)
async def read_session_realtime_latency(
    session_id: UUID,
    event_limit: int = Query(20, ge=1, le=100, description="Recent durable transcript events to inspect"),
    surface: str | None = Query(None, description="Optional client surface filter such as web or ios"),
    db: Session = Depends(get_db),
) -> RealtimePropagationSessionReportResponse:
    report = build_realtime_propagation_session_report(
        db,
        session_id=session_id,
        event_limit=event_limit,
        surface=surface,
    )
    if report is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return report


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
    db: Session = Depends(_machine_health_db_dependency),
) -> MachineHealthListResponse:
    summaries, total = list_machine_transport_health(
        db,
        device_id=device_id,
        status=status,
        stale_after_seconds=stale_after_seconds,
        recent_within_seconds=_resolve_recent_machine_window_seconds(
            recent_within_hours=recent_within_hours,
        ),
        limit=limit,
        **({"heartbeat_model": _machine_health_model} if _machine_health_model is not None else {}),
    )
    return build_machine_health_list_response(summaries, total=total)


@router.get("/turns/slow", response_model=SlowTurnsListResponse)
async def list_slow_turns(
    provider: str | None = Query(None, description="Filter by session provider"),
    project: str | None = Query(None, description="Filter by project"),
    device_id: str | None = Query(None, description="Filter by device"),
    state: str | None = Query(
        None,
        description=(
            "Filter by completed turn state (for example terminal|durable|failed). "
            "Only turns with terminal_at or durable_at are eligible."
        ),
    ),
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
) -> SlowTurnsListResponse:
    if materialize_recent_managed_transcript_turns(
        db,
        provider=provider,
        project=project,
        device_id=device_id,
        hours_back=hours_back,
    ):
        db.commit()

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
    return build_slow_turns_list_response(
        summaries,
        total=total,
        hours_back=hours_back,
        min_total_turn_time_ms=min_total_turn_time_ms,
    )


@router.get("/turns/summary", response_model=ManagedTurnsSummaryEnvelopeResponse)
async def summarize_turns(
    provider: str | None = Query(None, description="Filter by session provider"),
    project: str | None = Query(None, description="Filter by project"),
    device_id: str | None = Query(None, description="Filter by device"),
    state: str | None = Query(
        None,
        description=(
            "Filter by completed turn state (for example terminal|durable|failed). "
            "Only turns with terminal_at or durable_at are eligible."
        ),
    ),
    machine_status: MachineHealthStatus | None = Query(None, description="Filter by current machine transport state"),
    slow_threshold_ms: int = Query(
        30_000,
        ge=1_000,
        le=60 * 60 * 1_000,
        description="Count turns at or above this total duration as slow",
    ),
    hours_back: int = Query(
        24,
        ge=1,
        le=24 * 7,
        description="Only consider completed turns submitted within this recent window",
    ),
    stale_after_seconds: int = Query(
        DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
        ge=60,
        le=24 * 60 * 60,
        description="Treat heartbeats older than this as offline when enriching machine status",
    ),
    db: Session = Depends(get_db),
) -> ManagedTurnsSummaryEnvelopeResponse:
    if materialize_recent_managed_transcript_turns(
        db,
        provider=provider,
        project=project,
        device_id=device_id,
        hours_back=hours_back,
    ):
        db.commit()

    summaries = list_managed_completed_turns(
        db,
        provider=provider,
        project=project,
        device_id=device_id,
        state=state,
        machine_status=machine_status,
        hours_back=hours_back,
        stale_after_seconds=stale_after_seconds,
    )
    return build_managed_turns_summary_envelope_response(
        summaries,
        hours_back=hours_back,
        slow_threshold_ms=slow_threshold_ms,
    )


@router.get("/overview", response_model=ObservabilityOverviewResponse)
async def read_observability_overview(
    provider: str | None = Query(None, description="Filter turn telemetry by session provider"),
    project: str | None = Query(None, description="Filter turn telemetry by project"),
    device_id: str | None = Query(None, description="Filter machines and turns by device"),
    state: str | None = Query(
        None,
        description=(
            "Filter completed turns by state (for example terminal|durable|failed). "
            "Only turns with terminal_at or durable_at are eligible."
        ),
    ),
    machine_status: MachineHealthStatus | None = Query(
        None,
        description="Filter both the machine list and turn enrichment by machine transport state",
    ),
    slow_threshold_ms: int = Query(
        30_000,
        ge=1_000,
        le=60 * 60 * 1_000,
        description="Count turns at or above this total duration as slow",
    ),
    hours_back: int = Query(
        24,
        ge=1,
        le=24 * 7,
        description="Only consider recent completed turns in this lookback window",
    ),
    machine_limit: int = Query(8, ge=1, le=100, description="Max machine rows to include in the overview"),
    slow_turn_limit: int = Query(8, ge=1, le=100, description="Max slow-turn rows to include in the overview"),
    stale_after_seconds: int = Query(
        DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
        ge=60,
        le=24 * 60 * 60,
        description="Treat heartbeats older than this as offline when enriching machine status",
    ),
    recent_within_hours: int = Query(
        DEFAULT_MACHINE_HEALTH_RECENT_WITHIN_SECONDS // 3600,
        ge=1,
        le=24 * 30,
        description="Only include machines with a heartbeat in this recent window",
    ),
    db: Session = Depends(get_db),
) -> ObservabilityOverviewResponse:
    if materialize_recent_managed_transcript_turns(
        db,
        provider=provider,
        project=project,
        device_id=device_id,
        hours_back=hours_back,
    ):
        db.commit()

    recent_within_seconds = _resolve_recent_machine_window_seconds(
        recent_within_hours=recent_within_hours,
    )
    turn_summaries = list_managed_completed_turns(
        db,
        provider=provider,
        project=project,
        device_id=device_id,
        state=state,
        machine_status=machine_status,
        hours_back=hours_back,
        stale_after_seconds=stale_after_seconds,
    )
    machine_summaries, _ = list_machine_transport_health(
        db,
        device_id=device_id,
        status=machine_status,
        stale_after_seconds=stale_after_seconds,
        recent_within_seconds=recent_within_seconds,
        limit=10_000,
    )
    return build_observability_overview_response(
        turn_summaries=turn_summaries,
        machine_summaries=machine_summaries,
        hours_back=hours_back,
        slow_threshold_ms=slow_threshold_ms,
        stale_after_seconds=stale_after_seconds,
        machine_limit=machine_limit,
        slow_turn_limit=slow_turn_limit,
    )
