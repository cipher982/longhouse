"""Shared observability response models for machine and browser surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from zerg.services.session_views import SessionTurnTimingResponse
from zerg.utils.time import UTCBaseModel

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
    last_ship_error_kind: str | None = None
    last_ship_error_message: str | None = None
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


class MachineHealthStatusCountsResponse(UTCBaseModel):
    total: int = Field(
        0,
        description="Total machines in the current filtered slice before machine_limit is applied.",
    )
    healthy: int = 0
    degraded: int = 0
    offline: int = 0
    broken: int = 0


class ObservabilityOverviewResponse(UTCBaseModel):
    generated_at: datetime
    hours_back: int
    slow_threshold_ms: int
    stale_after_seconds: int
    summary: ManagedTurnSummaryResponse
    providers: list[ManagedTurnProviderSummaryResponse]
    machines: list[MachineHealthItemResponse] = Field(
        ...,
        description="Machine rows in the current filtered slice, truncated by machine_limit.",
    )
    machine_counts: MachineHealthStatusCountsResponse = Field(
        ...,
        description="Status counts for the current filtered machine slice before machine_limit truncation.",
    )
    slow_turns: list[SlowTurnItemResponse] = Field(
        ...,
        description="Slow-turn rows in the current filtered turn slice, truncated by slow_turn_limit.",
    )
    slow_turn_total: int = Field(
        ...,
        description="Total slow turns in the current filtered turn slice before slow_turn_limit truncation.",
    )
