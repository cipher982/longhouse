"""Cross-session managed turn observability endpoints."""

from __future__ import annotations

from collections import defaultdict
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
from zerg.services.session_turns import ManagedCompletedTurnSummary
from zerg.services.session_turns import list_managed_completed_turns
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


class TurnLatencyPercentilesResponse(UTCBaseModel):
    p50: int | None = None
    p95: int | None = None
    max: int | None = None


class ManagedTurnSummaryResponse(UTCBaseModel):
    completed_turns: int
    slow_turns: int
    durable_turns: int
    terminal_only_turns: int
    submit_to_send_ms: TurnLatencyPercentilesResponse
    submit_to_active_ms: TurnLatencyPercentilesResponse
    submit_to_terminal_ms: TurnLatencyPercentilesResponse
    active_to_terminal_ms: TurnLatencyPercentilesResponse
    terminal_to_durable_ms: TurnLatencyPercentilesResponse
    total_turn_time_ms: TurnLatencyPercentilesResponse


class ManagedTurnProviderSummaryResponse(ManagedTurnSummaryResponse):
    provider: str


class ManagedTurnsSummaryEnvelopeResponse(UTCBaseModel):
    hours_back: int
    slow_threshold_ms: int
    summary: ManagedTurnSummaryResponse
    providers: list[ManagedTurnProviderSummaryResponse]


@router.get("/slow", response_model=SlowTurnsListResponse)
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


@router.get("/summary", response_model=ManagedTurnsSummaryEnvelopeResponse)
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
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ManagedTurnsSummaryEnvelopeResponse:
    """Summarize recent completed managed turns overall and by provider."""

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
    grouped: dict[str, list[ManagedCompletedTurnSummary]] = defaultdict(list)
    for item in summaries:
        grouped[item.session.provider].append(item)

    provider_rows = [
        ManagedTurnProviderSummaryResponse(
            provider=provider_key,
            **_build_turn_summary(group_items, slow_threshold_ms=slow_threshold_ms).model_dump(),
        )
        for provider_key, group_items in sorted(
            grouped.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    ]

    return ManagedTurnsSummaryEnvelopeResponse(
        hours_back=hours_back,
        slow_threshold_ms=slow_threshold_ms,
        summary=_build_turn_summary(summaries, slow_threshold_ms=slow_threshold_ms),
        providers=provider_rows,
    )


def _build_turn_summary(
    summaries: list[ManagedCompletedTurnSummary],
    *,
    slow_threshold_ms: int,
) -> ManagedTurnSummaryResponse:
    timing_fields = {
        "submit_to_send_ms": [],
        "submit_to_active_ms": [],
        "submit_to_terminal_ms": [],
        "active_to_terminal_ms": [],
        "terminal_to_durable_ms": [],
        "total_turn_time_ms": [],
    }
    durable_turns = 0
    terminal_only_turns = 0
    slow_turns = 0

    for item in summaries:
        timing = build_session_turn_timing_response(item.turn)
        for field_name, values in timing_fields.items():
            value = getattr(timing, field_name)
            if value is not None:
                values.append(int(value))
        if item.turn.durable_at is not None:
            durable_turns += 1
        else:
            terminal_only_turns += 1
        if item.total_turn_time_ms >= slow_threshold_ms:
            slow_turns += 1

    return ManagedTurnSummaryResponse(
        completed_turns=len(summaries),
        slow_turns=slow_turns,
        durable_turns=durable_turns,
        terminal_only_turns=terminal_only_turns,
        submit_to_send_ms=_build_latency_percentiles(timing_fields["submit_to_send_ms"]),
        submit_to_active_ms=_build_latency_percentiles(timing_fields["submit_to_active_ms"]),
        submit_to_terminal_ms=_build_latency_percentiles(timing_fields["submit_to_terminal_ms"]),
        active_to_terminal_ms=_build_latency_percentiles(timing_fields["active_to_terminal_ms"]),
        terminal_to_durable_ms=_build_latency_percentiles(timing_fields["terminal_to_durable_ms"]),
        total_turn_time_ms=_build_latency_percentiles(timing_fields["total_turn_time_ms"]),
    )


def _build_latency_percentiles(values: list[int]) -> TurnLatencyPercentilesResponse:
    clean_values = sorted(int(value) for value in values if value is not None)
    if not clean_values:
        return TurnLatencyPercentilesResponse()
    return TurnLatencyPercentilesResponse(
        p50=_percentile(clean_values, 50),
        p95=_percentile(clean_values, 95),
        max=clean_values[-1],
    )


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    k = (len(values) - 1) * (percentile / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return int(values[f])
    d0 = values[f] * (c - k)
    d1 = values[c] * (k - f)
    return int(round(d0 + d1))
