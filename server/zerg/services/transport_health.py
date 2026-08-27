"""Canonical transport-health reduction for Longhouse machine shipping."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    # Only used as a type hint below. Importing at runtime pulls in the full
    # SQLAlchemy models package, which requires DATABASE_URL and breaks
    # CLI-only entrypoints like `longhouse local-health`.
    from zerg.models.agents import AgentHeartbeat

TRANSPORT_ERROR_DEGRADED_MIN_COUNT = 3
TRANSPORT_ERROR_DEGRADED_MIN_RATE = 0.25
CURRENT_TRANSPORT_ERROR_DEGRADED_MIN_COUNT = 2
CONSECUTIVE_FAILURES_DEGRADED_MIN_COUNT = 2
# Long enough that an idle laptop between sessions is not called unhealthy, short
# enough that a real outage surfaces the same day. cinder's went 33 hours.
SHIP_STALLED_DEGRADED_MIN_SECONDS = 6 * 60 * 60
ACTIVE_TRANSPORT_WINDOW_LABEL = "last 10 minutes"


def _normalize_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _normalize_optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class TransportHealthSample:
    spool_pending: int = 0
    spool_dead: int = 0
    parse_errors_1h: int = 0
    consecutive_failures: int = 0
    ship_attempts_1h: int = 0
    ship_successes_1h: int = 0
    ship_rate_limited_1h: int = 0
    ship_server_errors_1h: int = 0
    ship_payload_rejections_1h: int = 0
    ship_payload_too_large_1h: int = 0
    ship_retryable_client_errors_1h: int = 0
    ship_connect_errors_1h: int = 0
    ship_attempts_10m: int | None = None
    ship_successes_10m: int | None = None
    ship_rate_limited_10m: int | None = None
    ship_server_errors_10m: int | None = None
    ship_retryable_client_errors_10m: int | None = None
    ship_connect_errors_10m: int | None = None
    last_ship_result: str | None = None
    last_ship_http_status: int | None = None
    last_ship_error_kind: str | None = None
    last_ship_error_message: str | None = None
    is_offline: bool = False
    # The only elapsed-time inputs this assessment has. Without them it can
    # answer "are ships failing?" but not "has anything shipped at all?" — and a
    # machine whose heartbeats are current while its last successful ship was 33
    # hours ago classified healthy, which is exactly how one went unnoticed.
    last_ship_at: datetime | None = None
    observed_at: datetime | None = None

    @property
    def seconds_since_last_ship(self) -> float | None:
        """Age of the last successful ship, or None when it cannot be known.

        Absent rather than zero when either timestamp is missing: a machine that
        has never shipped is not the same as one that shipped a moment ago, and
        guessing either way would put a fabricated number into a health verdict.
        """

        if self.last_ship_at is None:
            return None
        last_ship_at = self.last_ship_at
        # Builders stay pure: neither stamps a clock reading, so the two remain
        # directly comparable. "Now" is only supplied here, where the question
        # is actually being asked.
        observed_at = self.observed_at or datetime.now(timezone.utc)
        if last_ship_at.tzinfo is None:
            last_ship_at = last_ship_at.replace(tzinfo=timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        return max(0.0, (observed_at - last_ship_at).total_seconds())

    @property
    def ship_success_rate_1h(self) -> float | None:
        if self.ship_attempts_1h <= 0:
            return None
        return round(self.ship_successes_1h / self.ship_attempts_1h, 4)

    @property
    def has_active_window(self) -> bool:
        return self.ship_attempts_10m is not None

    @property
    def ship_attempts_active(self) -> int:
        return self.ship_attempts_10m if self.ship_attempts_10m is not None else self.ship_attempts_1h

    @property
    def ship_connect_errors_active(self) -> int:
        return self.ship_connect_errors_10m if self.ship_connect_errors_10m is not None else self.ship_connect_errors_1h

    @property
    def ship_server_errors_active(self) -> int:
        return self.ship_server_errors_10m if self.ship_server_errors_10m is not None else self.ship_server_errors_1h

    @property
    def ship_rate_limited_active(self) -> int:
        return self.ship_rate_limited_10m if self.ship_rate_limited_10m is not None else self.ship_rate_limited_1h

    @property
    def ship_retryable_client_errors_active(self) -> int:
        if self.ship_retryable_client_errors_10m is not None:
            return self.ship_retryable_client_errors_10m
        return self.ship_retryable_client_errors_1h


@dataclass(frozen=True)
class TransportHealthAssessment:
    status: str
    status_reason: str
    status_summary: str
    reasons: tuple[str, ...]


def transport_health_sample_from_heartbeat(row: AgentHeartbeat) -> TransportHealthSample:
    raw = _heartbeat_raw_json(row)
    last_ship_http_status = getattr(row, "last_ship_http_status", None) or raw.get("last_ship_http_status")
    return TransportHealthSample(
        spool_pending=_normalize_int(getattr(row, "spool_pending", 0)),
        spool_dead=_normalize_int(getattr(row, "spool_dead", 0)),
        parse_errors_1h=_normalize_int(getattr(row, "parse_errors_1h", 0)),
        consecutive_failures=_normalize_int(getattr(row, "consecutive_failures", 0)),
        ship_attempts_1h=_normalize_int(getattr(row, "ship_attempts_1h", 0)),
        ship_successes_1h=_normalize_int(getattr(row, "ship_successes_1h", 0)),
        ship_rate_limited_1h=_normalize_int(getattr(row, "ship_rate_limited_1h", 0)),
        ship_server_errors_1h=_normalize_int(getattr(row, "ship_server_errors_1h", 0)),
        ship_payload_rejections_1h=_normalize_int(getattr(row, "ship_payload_rejections_1h", 0)),
        ship_payload_too_large_1h=_normalize_int(getattr(row, "ship_payload_too_large_1h", 0)),
        ship_retryable_client_errors_1h=_normalize_int(getattr(row, "ship_retryable_client_errors_1h", 0)),
        ship_connect_errors_1h=_normalize_int(getattr(row, "ship_connect_errors_1h", 0)),
        ship_attempts_10m=_normalize_present_int(raw, "ship_attempts_10m"),
        ship_successes_10m=_normalize_present_int(raw, "ship_successes_10m"),
        ship_rate_limited_10m=_normalize_present_int(raw, "ship_rate_limited_10m"),
        ship_server_errors_10m=_normalize_present_int(raw, "ship_server_errors_10m"),
        ship_retryable_client_errors_10m=_normalize_present_int(raw, "ship_retryable_client_errors_10m"),
        ship_connect_errors_10m=_normalize_present_int(raw, "ship_connect_errors_10m"),
        last_ship_result=_normalize_optional_str(getattr(row, "last_ship_result", None) or raw.get("last_ship_result")),
        last_ship_http_status=_normalize_optional_int(last_ship_http_status),
        last_ship_error_kind=_normalize_optional_str(raw.get("last_ship_error_kind")),
        last_ship_error_message=_normalize_optional_str(raw.get("last_ship_error_message")),
        is_offline=bool(getattr(row, "is_offline", False)),
        last_ship_at=_normalize_optional_datetime(getattr(row, "last_ship_at", None) or raw.get("last_ship_at")),
    )


def transport_health_sample_from_engine_status_payload(payload: Mapping[str, Any] | None) -> TransportHealthSample:
    raw_payload = payload if isinstance(payload, Mapping) else {}
    return TransportHealthSample(
        spool_pending=_normalize_int(raw_payload.get("spool_pending_count")),
        spool_dead=_normalize_int(raw_payload.get("spool_dead_count")),
        parse_errors_1h=_normalize_int(raw_payload.get("parse_error_count_1h")),
        consecutive_failures=_normalize_int(raw_payload.get("consecutive_ship_failures")),
        last_ship_at=_normalize_optional_datetime(raw_payload.get("last_ship_at")),
        ship_attempts_1h=_normalize_int(raw_payload.get("ship_attempts_1h")),
        ship_successes_1h=_normalize_int(raw_payload.get("ship_successes_1h")),
        ship_rate_limited_1h=_normalize_int(raw_payload.get("ship_rate_limited_1h")),
        ship_server_errors_1h=_normalize_int(raw_payload.get("ship_server_errors_1h")),
        ship_payload_rejections_1h=_normalize_int(raw_payload.get("ship_payload_rejections_1h")),
        ship_payload_too_large_1h=_normalize_int(raw_payload.get("ship_payload_too_large_1h")),
        ship_retryable_client_errors_1h=_normalize_int(raw_payload.get("ship_retryable_client_errors_1h")),
        ship_connect_errors_1h=_normalize_int(raw_payload.get("ship_connect_errors_1h")),
        ship_attempts_10m=_normalize_present_int(raw_payload, "ship_attempts_10m"),
        ship_successes_10m=_normalize_present_int(raw_payload, "ship_successes_10m"),
        ship_rate_limited_10m=_normalize_present_int(raw_payload, "ship_rate_limited_10m"),
        ship_server_errors_10m=_normalize_present_int(raw_payload, "ship_server_errors_10m"),
        ship_retryable_client_errors_10m=_normalize_present_int(raw_payload, "ship_retryable_client_errors_10m"),
        ship_connect_errors_10m=_normalize_present_int(raw_payload, "ship_connect_errors_10m"),
        last_ship_result=_normalize_optional_str(raw_payload.get("last_ship_result")),
        last_ship_http_status=_normalize_optional_int(raw_payload.get("last_ship_http_status")),
        last_ship_error_kind=_normalize_optional_str(raw_payload.get("last_ship_error_kind")),
        last_ship_error_message=_normalize_optional_str(raw_payload.get("last_ship_error_message")),
        is_offline=bool(raw_payload.get("is_offline", False)),
    )


def is_transport_error_burst(
    *,
    error_count: int,
    ship_attempts: int,
    last_ship_result: str | None,
    result_kind: str,
) -> bool:
    """Return True for current failure or sustained transport noise."""
    if error_count <= 0:
        return False
    if last_ship_result == result_kind and error_count >= CURRENT_TRANSPORT_ERROR_DEGRADED_MIN_COUNT:
        return True
    if result_kind != "connect_error":
        return False
    if ship_attempts <= 0:
        return False
    if error_count < TRANSPORT_ERROR_DEGRADED_MIN_COUNT:
        return False
    return (error_count / ship_attempts) >= TRANSPORT_ERROR_DEGRADED_MIN_RATE


def _transport_window_phrase(sample: TransportHealthSample) -> str:
    return f"in the {ACTIVE_TRANSPORT_WINDOW_LABEL}" if sample.has_active_window else "in the last hour"


def _humanize_age(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours >= 24:
        days = hours // 24
        return f"{days} day(s)"
    if hours >= 1:
        return f"{hours} hour(s)"
    return f"{int(seconds // 60)} minute(s)"


def assess_transport_health(sample: TransportHealthSample) -> TransportHealthAssessment:
    # A live machine that has stopped shipping is the failure this assessment
    # could not previously express: every other input asks whether ship attempts
    # are erroring, and a machine whose attempts stopped happening entirely has
    # no errors to report. Only counted when the agent is demonstrably present —
    # an offline machine is already described by being offline.
    stalled_age = sample.seconds_since_last_ship
    ship_stalled = (
        not sample.is_offline
        and stalled_age is not None
        and stalled_age >= SHIP_STALLED_DEGRADED_MIN_SECONDS
    )
    connect_error_burst = is_transport_error_burst(
        error_count=sample.ship_connect_errors_active,
        ship_attempts=sample.ship_attempts_active,
        last_ship_result=sample.last_ship_result,
        result_kind="connect_error",
    )
    server_error_burst = is_transport_error_burst(
        error_count=sample.ship_server_errors_active,
        ship_attempts=sample.ship_attempts_active,
        last_ship_result=sample.last_ship_result,
        result_kind="server_error",
    )
    rate_limited_burst = is_transport_error_burst(
        error_count=sample.ship_rate_limited_active,
        ship_attempts=sample.ship_attempts_active,
        last_ship_result=sample.last_ship_result,
        result_kind="rate_limited",
    )
    retryable_client_error_burst = is_transport_error_burst(
        error_count=sample.ship_retryable_client_errors_active,
        ship_attempts=sample.ship_attempts_active,
        last_ship_result=sample.last_ship_result,
        result_kind="retryable_client_error",
    )

    reasons: list[str] = []
    if sample.is_offline:
        reasons.append("reported_offline")
    if sample.spool_dead > 0:
        reasons.append("spool_dead")
    if sample.ship_payload_rejections_1h > 0:
        reasons.append("payload_rejected")
    if sample.ship_payload_too_large_1h > 0:
        reasons.append("payload_too_large")
    if sample.parse_errors_1h > 0:
        reasons.append("parse_errors")
    if sample.consecutive_failures >= CONSECUTIVE_FAILURES_DEGRADED_MIN_COUNT:
        reasons.append("consecutive_failures")
    if ship_stalled:
        reasons.append("ship_stalled")
    if connect_error_burst:
        reasons.append("connect_errors")
    if server_error_burst:
        reasons.append("server_errors")
    if rate_limited_burst:
        reasons.append("rate_limited")
    if retryable_client_error_burst:
        reasons.append("retryable_client_errors")
    if sample.ship_payload_rejections_1h > 0:
        status = "broken"
        status_reason = "payload_rejected"
        status_summary = f"{sample.ship_payload_rejections_1h} ship payload rejection(s) in the last hour."
    elif sample.ship_payload_too_large_1h > 0:
        status = "broken"
        status_reason = "payload_too_large"
        status_summary = f"{sample.ship_payload_too_large_1h} ship payload too-large rejection(s) in the last hour."
    elif sample.is_offline:
        status = "offline"
        status_reason = "reported_offline"
        status_summary = "Engine reported offline."
    elif sample.spool_dead > 0:
        status = "degraded"
        status_reason = "spool_dead"
        status_summary = f"{sample.spool_dead} dead-letter archive range(s) need attention."
    elif sample.parse_errors_1h > 0:
        status = "degraded"
        status_reason = "parse_errors"
        status_summary = f"{sample.parse_errors_1h} parse error(s) in the last hour."
    elif sample.consecutive_failures >= CONSECUTIVE_FAILURES_DEGRADED_MIN_COUNT:
        status = "degraded"
        status_reason = "consecutive_failures"
        status_summary = f"{sample.consecutive_failures} consecutive ship failure(s)."
    elif ship_stalled:
        status = "degraded"
        status_reason = "ship_stalled"
        status_summary = (
            f"Heartbeats are current but nothing has shipped for {_humanize_age(stalled_age)}."
        )
    elif connect_error_burst:
        status = "degraded"
        status_reason = "connect_errors"
        status_summary = _append_last_ship_error_detail(
            f"{sample.ship_connect_errors_active} ship connect error(s) {_transport_window_phrase(sample)}.",
            sample,
        )
    elif server_error_burst:
        status = "degraded"
        status_reason = "server_errors"
        status_summary = _append_last_ship_error_detail(
            f"{sample.ship_server_errors_active} ship server error(s) {_transport_window_phrase(sample)}.",
            sample,
        )
    elif rate_limited_burst:
        status = "degraded"
        status_reason = "rate_limited"
        status_summary = _append_last_ship_error_detail(
            f"{sample.ship_rate_limited_active} rate-limit response(s) {_transport_window_phrase(sample)}.",
            sample,
        )
    elif retryable_client_error_burst:
        status = "degraded"
        status_reason = "retryable_client_errors"
        retryable_window = _transport_window_phrase(sample)
        retryable_summary = f"{sample.ship_retryable_client_errors_active} retryable client error(s) {retryable_window}."
        status_summary = _append_last_ship_error_detail(
            retryable_summary,
            sample,
        )
    else:
        status = "healthy"
        status_reason = "healthy"
        status_summary = "Shipping healthy."

    return TransportHealthAssessment(
        status=status,
        status_reason=status_reason,
        status_summary=status_summary,
        reasons=tuple(reasons),
    )


def _normalize_optional_int(value: Any) -> int | None:
    normalized = _normalize_int(value)
    if value is None or str(value).strip() == "":
        return None
    return normalized


def _normalize_present_int(payload: Mapping[str, Any], key: str) -> int | None:
    if key not in payload:
        return None
    return _normalize_int(payload.get(key))


def _normalize_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _heartbeat_raw_json(row: AgentHeartbeat) -> Mapping[str, Any]:
    raw_json = getattr(row, "raw_json", None)
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _append_last_ship_error_detail(summary: str, sample: TransportHealthSample) -> str:
    if sample.last_ship_result not in {
        "connect_error",
        "server_error",
        "rate_limited",
        "retryable_client_error",
    }:
        return summary
    if sample.last_ship_error_kind:
        return f"{summary} Last error: {sample.last_ship_error_kind}."
    if sample.last_ship_http_status is not None:
        return f"{summary} Last HTTP status: {sample.last_ship_http_status}."
    return summary
