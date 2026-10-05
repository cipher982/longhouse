"""Shared observability response models for machine and browser surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import Field

from zerg.schemas.history_import import HistoryImportSnapshot
from zerg.utils.time import UTCBaseModel

MachineHealthStatus = Literal["healthy", "degraded", "offline", "broken", "unknown"]
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
    suggested_action_ids: list[str] = Field(default_factory=list)
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
    archive_repair: dict[str, Any] = Field(default_factory=dict)
    runtime_event_outbox: dict[str, Any] | None = None
    history_import: HistoryImportSnapshot = Field(default_factory=HistoryImportSnapshot.unavailable)
    parse_errors_1h: int
    disk_free_bytes: int
    is_offline: bool


class MachineHealthListResponse(UTCBaseModel):
    machines: list[MachineHealthItemResponse]
    total: int


class ProductHealthCheckSummaryResponse(UTCBaseModel):
    check: str
    verdict: ProductHealthCheckVerdict
    coverage: ProductHealthCheckCoverage
    window: str
    generated_at: datetime
    headline: str
    signals: dict[str, Any] | None = None
