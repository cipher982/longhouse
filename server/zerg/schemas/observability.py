"""Shared observability response models for machine and browser surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import Field

from zerg.services.session_views import SessionTurnTimingResponse
from zerg.utils.time import UTCBaseModel

MachineHealthStatus = Literal["healthy", "degraded", "offline", "broken"]
ProductHealthCheckVerdict = Literal["ok", "degraded", "failing", "unknown"]
ProductHealthCheckCoverage = Literal["full", "partial", "none"]


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
    ship_attempts_10m: int | None = None
    ship_successes_10m: int | None = None
    ship_rate_limited_10m: int | None = None
    ship_server_errors_10m: int | None = None
    ship_retryable_client_errors_10m: int | None = None
    ship_connect_errors_10m: int | None = None
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


class ProductHealthCheckThresholdsResponse(UTCBaseModel):
    render_p95_ms_ok: int
    render_p95_ms_failing: int


class ProductHealthCheckEvidenceRefResponse(UTCBaseModel):
    kind: str
    id: str
    reason: str
    latency_ms: int | None = None


class ProductHealthCheckLivePreviewDimensionResponse(UTCBaseModel):
    provider: str | None = None
    surface: str | None = None
    managed: bool | None = None


class ProductHealthCheckLivePreviewSignalsResponse(UTCBaseModel):
    events: int = 0
    sessions: int = 0
    render_p50_ms: int | None = None
    render_p95_ms: int | None = None
    render_max_ms: int | None = None
    ios_render_duration_events: int = 0
    ios_render_duration_p50_ms: int | None = None
    ios_render_duration_p95_ms: int | None = None
    ios_render_duration_max_ms: int | None = None


class ProductHealthCheckLivePreviewCellResponse(UTCBaseModel):
    dimension: ProductHealthCheckLivePreviewDimensionResponse
    applicable: bool
    coverage: ProductHealthCheckCoverage
    verdict: ProductHealthCheckVerdict
    truncated: bool = False
    signals: ProductHealthCheckLivePreviewSignalsResponse
    thresholds: ProductHealthCheckThresholdsResponse
    missing: list[str]
    evidence_refs: list[ProductHealthCheckEvidenceRefResponse]


class ProductHealthCheckSummaryResponse(UTCBaseModel):
    check: str
    verdict: ProductHealthCheckVerdict
    coverage: ProductHealthCheckCoverage
    window: str
    generated_at: datetime
    headline: str


class ProductHealthCheckListResponse(UTCBaseModel):
    checks: list[ProductHealthCheckSummaryResponse]


class ProductHealthCheckLivePreviewResponse(UTCBaseModel):
    check: Literal["live_preview"]
    window: str
    generated_at: datetime
    cells: list[ProductHealthCheckLivePreviewCellResponse]


RealtimePropagationStageStatus = Literal["measured", "missing"]
RealtimePropagationConfidence = Literal["observed", "derived", "missing"]


class RealtimePropagationSessionResponse(UTCBaseModel):
    session_id: str
    provider: str
    project: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    managed_transport: str | None = None
    execution_home: str | None = None
    started_at: datetime
    last_activity_at: datetime | None = None


class RealtimePropagationObservationRefResponse(UTCBaseModel):
    observation_id: str
    source: str
    kind: str
    observed_at: datetime
    received_at: datetime
    source_offset: int | None = None
    source_cursor: str | None = None


class RealtimePropagationShipTraceResponse(UTCBaseModel):
    trace_id: str
    work_context: str | None = None
    observation_source: str | None = None
    event_count: int | None = None
    offset: int | None = None
    new_offset: int | None = None
    range_bytes: int | None = None
    observed_at: datetime | None = None
    enqueued_at: datetime | None = None
    job_started_at: datetime | None = None
    http_send_started_at: datetime | None = None
    server_handler_entered_at: datetime | None = None
    server_store_returned_at: datetime | None = None
    observation_to_enqueue_ms: int | None = None
    enqueue_to_job_ms: int | None = None
    job_to_http_ms: int | None = None
    server_store_write_ms: int | None = None
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Trace timing metadata only. Transcript payload/content is not included.",
    )
    raw_dropped_keys: int = Field(0, description="Count of ship_trace keys omitted by the safe metadata allowlist.")


class RealtimePropagationClientRenderResponse(UTCBaseModel):
    surface: str
    event_id: str | None = None
    matched_by: str
    observed_at: datetime
    received_at: datetime
    emitted_at_ms: int | None = None
    rendered_at_ms: int | None = None
    clock_skew_ms: int | None = None
    server_fanout_at_ms: int | None = None
    client_received_at_ms: int | None = None
    pubsub_seq: int | None = None
    latency_ms: int | None = None
    webkit_stage: str | None = None
    latest_item_id: str | None = None


class RealtimePropagationServerFanoutResponse(UTCBaseModel):
    observation_id: str
    observed_at: datetime
    received_at: datetime
    latest_event_id: int | None = None
    server_fanout_at_ms: int | None = None
    session_pubsub_seq: int | None = None
    timeline_pubsub_seq: int | None = None
    ship_trace_id: str | None = None


class RealtimePropagationStageResponse(UTCBaseModel):
    key: str
    label: str
    status: RealtimePropagationStageStatus
    confidence: RealtimePropagationConfidence
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    source: str | None = None
    note: str | None = None


class RealtimePropagationBottleneckResponse(UTCBaseModel):
    stage_key: str
    label: str
    duration_ms: int


class RealtimePropagationEventResponse(UTCBaseModel):
    event_id: int
    role: str
    timestamp: datetime
    source_path: str | None = None
    source_offset: int | None = None
    event_uuid: str | None = None
    event_origin: str | None = None
    provider_observation: RealtimePropagationObservationRefResponse | None = None
    ship_trace: RealtimePropagationShipTraceResponse | None = None
    server_fanout: RealtimePropagationServerFanoutResponse | None = None
    client_renders: list[RealtimePropagationClientRenderResponse]
    first_client_render: RealtimePropagationClientRenderResponse | None = None
    total_provider_to_first_render_ms: int | None = None
    measured_total_ms: int | None = None
    unaccounted_ms: int | None = None
    client_clock_skew_ms: int | None = None
    bottleneck: RealtimePropagationBottleneckResponse | None = None
    stages: list[RealtimePropagationStageResponse]
    gaps: list[str]


class RealtimePropagationSessionReportResponse(UTCBaseModel):
    generated_at: datetime
    session: RealtimePropagationSessionResponse
    event_limit: int
    surface: str | None = None
    events: list[RealtimePropagationEventResponse]
    gaps: list[str]
    known_unimplemented_probes: list[str]
