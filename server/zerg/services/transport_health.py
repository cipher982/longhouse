"""Canonical transport-health reduction for Longhouse machine shipping."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
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
SHIPPING_PROGRESS_STALE_AFTER_SECONDS = 2 * 60
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
    ship_attempts_1h: int = 0
    ship_successes_1h: int = 0
    ship_rate_limited_1h: int = 0
    ship_server_errors_1h: int = 0
    ship_payload_rejections_1h: int = 0
    ship_payload_too_large_1h: int = 0
    ship_retryable_client_errors_1h: int = 0
    ship_connect_errors_1h: int = 0
    ship_attempts_10m: int = 0
    ship_successes_10m: int = 0
    ship_rate_limited_10m: int = 0
    ship_server_errors_10m: int = 0
    ship_retryable_client_errors_10m: int = 0
    ship_connect_errors_10m: int = 0
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
    evidence_available: bool = True
    shipping_progress_pending: bool | None = None
    shipping_progress_stalled: bool | None = None
    shipping_progress_seconds_without_progress: int | None = None
    shipping_progress_observed_at: datetime | None = None
    shipping_progress_valid: bool = False

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


@dataclass(frozen=True)
class TransportHealthAssessment:
    status: str
    status_reason: str
    status_summary: str
    reasons: tuple[str, ...]


def transport_health_sample_from_heartbeat(row: AgentHeartbeat) -> TransportHealthSample:
    raw = _heartbeat_raw_json(row)
    last_ship_http_status = getattr(row, "last_ship_http_status", None) or raw.get("last_ship_http_status")
    progress = _shipping_progress_from_payload(raw)
    return TransportHealthSample(
        spool_pending=_normalize_int(getattr(row, "spool_pending", 0)),
        spool_dead=_normalize_int(getattr(row, "spool_dead", 0)),
        parse_errors_1h=_normalize_int(getattr(row, "parse_errors_1h", 0)),
        ship_attempts_1h=_normalize_int(getattr(row, "ship_attempts_1h", 0)),
        ship_successes_1h=_normalize_int(getattr(row, "ship_successes_1h", 0)),
        ship_rate_limited_1h=_normalize_int(getattr(row, "ship_rate_limited_1h", 0)),
        ship_server_errors_1h=_normalize_int(getattr(row, "ship_server_errors_1h", 0)),
        ship_payload_rejections_1h=_normalize_int(getattr(row, "ship_payload_rejections_1h", 0)),
        ship_payload_too_large_1h=_normalize_int(getattr(row, "ship_payload_too_large_1h", 0)),
        ship_retryable_client_errors_1h=_normalize_int(getattr(row, "ship_retryable_client_errors_1h", 0)),
        ship_connect_errors_1h=_normalize_int(getattr(row, "ship_connect_errors_1h", 0)),
        ship_attempts_10m=_normalize_int(raw.get("ship_attempts_10m")),
        ship_successes_10m=_normalize_int(raw.get("ship_successes_10m")),
        ship_rate_limited_10m=_normalize_int(raw.get("ship_rate_limited_10m")),
        ship_server_errors_10m=_normalize_int(raw.get("ship_server_errors_10m")),
        ship_retryable_client_errors_10m=_normalize_int(raw.get("ship_retryable_client_errors_10m")),
        ship_connect_errors_10m=_normalize_int(raw.get("ship_connect_errors_10m")),
        last_ship_result=_normalize_optional_str(getattr(row, "last_ship_result", None) or raw.get("last_ship_result")),
        last_ship_http_status=_normalize_optional_int(last_ship_http_status),
        last_ship_error_kind=_normalize_optional_str(raw.get("last_ship_error_kind")),
        last_ship_error_message=_normalize_optional_str(raw.get("last_ship_error_message")),
        is_offline=bool(getattr(row, "is_offline", False)),
        last_ship_at=_normalize_optional_datetime(getattr(row, "last_ship_at", None) or raw.get("last_ship_at")),
        observed_at=_normalize_optional_datetime(getattr(row, "received_at", None)),
        shipping_progress_pending=progress[0],
        shipping_progress_stalled=progress[1],
        shipping_progress_seconds_without_progress=progress[2],
        shipping_progress_observed_at=progress[3],
        shipping_progress_valid=progress[4],
    )


def transport_health_sample_from_engine_status_payload(payload: Mapping[str, Any] | None) -> TransportHealthSample:
    raw_payload = payload if isinstance(payload, Mapping) else {}
    progress = _shipping_progress_from_payload(raw_payload)
    evidence_available = any(
        key in raw_payload for key in ("ship_attempts_1h", "ship_attempts_10m", "last_ship_result", "shipping_progress")
    )
    return TransportHealthSample(
        spool_pending=_normalize_int(raw_payload.get("spool_pending_count")),
        spool_dead=_normalize_int(raw_payload.get("spool_dead_count")),
        parse_errors_1h=_normalize_int(raw_payload.get("parse_error_count_1h")),
        last_ship_at=_normalize_optional_datetime(raw_payload.get("last_ship_at")),
        ship_attempts_1h=_normalize_int(raw_payload.get("ship_attempts_1h")),
        ship_successes_1h=_normalize_int(raw_payload.get("ship_successes_1h")),
        ship_rate_limited_1h=_normalize_int(raw_payload.get("ship_rate_limited_1h")),
        ship_server_errors_1h=_normalize_int(raw_payload.get("ship_server_errors_1h")),
        ship_payload_rejections_1h=_normalize_int(raw_payload.get("ship_payload_rejections_1h")),
        ship_payload_too_large_1h=_normalize_int(raw_payload.get("ship_payload_too_large_1h")),
        ship_retryable_client_errors_1h=_normalize_int(raw_payload.get("ship_retryable_client_errors_1h")),
        ship_connect_errors_1h=_normalize_int(raw_payload.get("ship_connect_errors_1h")),
        ship_attempts_10m=_normalize_int(raw_payload.get("ship_attempts_10m")),
        ship_successes_10m=_normalize_int(raw_payload.get("ship_successes_10m")),
        ship_rate_limited_10m=_normalize_int(raw_payload.get("ship_rate_limited_10m")),
        ship_server_errors_10m=_normalize_int(raw_payload.get("ship_server_errors_10m")),
        ship_retryable_client_errors_10m=_normalize_int(raw_payload.get("ship_retryable_client_errors_10m")),
        ship_connect_errors_10m=_normalize_int(raw_payload.get("ship_connect_errors_10m")),
        last_ship_result=_normalize_optional_str(raw_payload.get("last_ship_result")),
        last_ship_http_status=_normalize_optional_int(raw_payload.get("last_ship_http_status")),
        last_ship_error_kind=_normalize_optional_str(raw_payload.get("last_ship_error_kind")),
        last_ship_error_message=_normalize_optional_str(raw_payload.get("last_ship_error_message")),
        is_offline=bool(raw_payload.get("is_offline", False)),
        evidence_available=evidence_available,
        shipping_progress_pending=progress[0],
        shipping_progress_stalled=progress[1],
        shipping_progress_seconds_without_progress=progress[2],
        shipping_progress_observed_at=progress[3],
        shipping_progress_valid=progress[4],
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


def _transport_window_phrase() -> str:
    return f"in the {ACTIVE_TRANSPORT_WINDOW_LABEL}"


def _humanize_age(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours >= 24:
        days = hours // 24
        return f"{days} day(s)"
    if hours >= 1:
        return f"{hours} hour(s)"
    return f"{int(seconds // 60)} minute(s)"


def assess_transport_health(sample: TransportHealthSample) -> TransportHealthAssessment:
    if not sample.evidence_available:
        return TransportHealthAssessment(
            status="unknown",
            status_reason="transport_unavailable",
            status_summary="Shipping transport evidence unavailable.",
            reasons=("transport_unavailable",),
        )
    shipping_progress_unknown = (not sample.shipping_progress_valid or _shipping_progress_is_stale(sample)) and not sample.is_offline
    # The daemon owns the monotonic pending-work observation. A retained wall
    # clock is historical evidence only and must never turn an idle machine red.
    stalled_age = sample.shipping_progress_seconds_without_progress
    ship_stalled = (
        not sample.is_offline
        and not shipping_progress_unknown
        and sample.shipping_progress_pending is True
        and sample.shipping_progress_stalled is True
    )
    connect_error_burst = is_transport_error_burst(
        error_count=sample.ship_connect_errors_10m,
        ship_attempts=sample.ship_attempts_10m,
        last_ship_result=sample.last_ship_result,
        result_kind="connect_error",
    )
    server_error_burst = is_transport_error_burst(
        error_count=sample.ship_server_errors_10m,
        ship_attempts=sample.ship_attempts_10m,
        last_ship_result=sample.last_ship_result,
        result_kind="server_error",
    )
    rate_limited_burst = is_transport_error_burst(
        error_count=sample.ship_rate_limited_10m,
        ship_attempts=sample.ship_attempts_10m,
        last_ship_result=sample.last_ship_result,
        result_kind="rate_limited",
    )
    retryable_client_error_burst = is_transport_error_burst(
        error_count=sample.ship_retryable_client_errors_10m,
        ship_attempts=sample.ship_attempts_10m,
        last_ship_result=sample.last_ship_result,
        result_kind="retryable_client_error",
    )

    reasons: list[str] = []
    if sample.is_offline:
        reasons.append("reported_offline")
    if shipping_progress_unknown:
        reasons.append("transport_unavailable")
    if sample.spool_dead > 0:
        reasons.append("spool_dead")
    if sample.ship_payload_rejections_1h > 0:
        reasons.append("payload_rejected")
    if sample.ship_payload_too_large_1h > 0:
        reasons.append("payload_too_large")
    if sample.parse_errors_1h > 0:
        reasons.append("parse_errors")
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
    elif ship_stalled:
        status = "degraded"
        status_reason = "ship_stalled"
        status_summary = f"Pending shipping has made no useful progress for {_humanize_age(float(stalled_age or 0))}."
    elif connect_error_burst:
        status = "degraded"
        status_reason = "connect_errors"
        status_summary = _append_last_ship_error_detail(
            f"{sample.ship_connect_errors_10m} ship connect error(s) {_transport_window_phrase()}.",
            sample,
        )
    elif server_error_burst:
        status = "degraded"
        status_reason = "server_errors"
        status_summary = _append_last_ship_error_detail(
            f"{sample.ship_server_errors_10m} ship server error(s) {_transport_window_phrase()}.",
            sample,
        )
    elif rate_limited_burst:
        status = "degraded"
        status_reason = "rate_limited"
        status_summary = _append_last_ship_error_detail(
            f"{sample.ship_rate_limited_10m} rate-limit response(s) {_transport_window_phrase()}.",
            sample,
        )
    elif retryable_client_error_burst:
        status = "degraded"
        status_reason = "retryable_client_errors"
        retryable_window = _transport_window_phrase()
        retryable_summary = f"{sample.ship_retryable_client_errors_10m} retryable client error(s) {retryable_window}."
        status_summary = _append_last_ship_error_detail(
            retryable_summary,
            sample,
        )
    elif shipping_progress_unknown:
        status = "unknown"
        status_reason = "transport_unavailable"
        status_summary = "Shipping progress evidence unavailable."
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


def _shipping_progress_from_payload(
    payload: Mapping[str, Any],
) -> tuple[bool | None, bool | None, int | None, datetime | None, bool]:
    if "shipping_progress" not in payload:
        return None, None, None, None, False
    raw = payload.get("shipping_progress")
    if not isinstance(raw, Mapping):
        return None, None, None, None, False
    pending_work = raw.get("pending_work")
    stalled = raw.get("stalled")
    seconds_without_progress = raw.get("seconds_without_progress")
    observed_at = _normalize_optional_datetime(raw.get("observed_at"))
    if (
        not isinstance(pending_work, bool)
        or not isinstance(stalled, bool)
        or isinstance(seconds_without_progress, bool)
        or not isinstance(seconds_without_progress, int)
        or seconds_without_progress < 0
        or observed_at is None
    ):
        return None, None, None, None, False
    return pending_work, stalled, seconds_without_progress, observed_at, True


def _shipping_progress_is_stale(sample: TransportHealthSample) -> bool:
    if sample.shipping_progress_observed_at is None or sample.observed_at is None:
        return False
    observed_at = sample.observed_at
    progress_observed_at = sample.shipping_progress_observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    if progress_observed_at.tzinfo is None:
        progress_observed_at = progress_observed_at.replace(tzinfo=timezone.utc)
    return (observed_at - progress_observed_at).total_seconds() > SHIPPING_PROGRESS_STALE_AFTER_SECONDS


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
