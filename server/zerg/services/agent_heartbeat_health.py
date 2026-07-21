"""Machine-facing transport health summaries derived from latest heartbeats."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.models.agents import AgentHeartbeat
from zerg.schemas.history_import import HistoryImportSnapshot
from zerg.services.archive_backlog import default_archive_backlog
from zerg.services.archive_backlog import normalize_archive_backlog
from zerg.services.transport_health import TransportHealthAssessment
from zerg.services.transport_health import assess_transport_health
from zerg.services.transport_health import transport_health_sample_from_heartbeat
from zerg.utils.time import normalize_utc
from zerg.utils.time import utc_now

DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS = 15 * 60
DEFAULT_MACHINE_HEALTH_RECENT_WITHIN_SECONDS = 72 * 60 * 60

_STATE_SORT_ORDER = {
    "broken": 0,
    "offline": 1,
    "degraded": 2,
    "healthy": 3,
}


@dataclass(frozen=True)
class MachineTransportHealthSummary:
    device_id: str
    version: str | None
    last_heartbeat_at: datetime
    heartbeat_age_seconds: int
    stale_after_seconds: int
    is_stale: bool
    status: str
    status_reason: str
    status_summary: str
    reasons: tuple[str, ...]
    last_ship_at: datetime | None
    last_ship_attempt_at: datetime | None
    last_ship_result: str | None
    last_ship_latency_ms: int | None
    last_ship_http_status: int | None
    last_ship_error_kind: str | None
    last_ship_error_message: str | None
    ship_attempts_1h: int
    ship_successes_1h: int
    ship_success_rate_1h: float | None
    ship_rate_limited_1h: int
    ship_server_errors_1h: int
    ship_payload_rejections_1h: int
    ship_payload_too_large_1h: int
    ship_retryable_client_errors_1h: int
    ship_connect_errors_1h: int
    ship_latency_p50_ms_1h: int | None
    ship_latency_p95_ms_1h: int | None
    ship_attempts_10m: int | None
    ship_successes_10m: int | None
    ship_rate_limited_10m: int | None
    ship_server_errors_10m: int | None
    ship_retryable_client_errors_10m: int | None
    ship_connect_errors_10m: int | None
    spool_pending: int
    spool_dead: int
    archive_repair: dict[str, Any]
    history_import: HistoryImportSnapshot
    parse_errors_1h: int
    consecutive_failures: int
    disk_free_bytes: int
    is_offline: bool


def list_machine_transport_health(
    db: Session,
    *,
    device_id: str | None = None,
    status: str | None = None,
    stale_after_seconds: int = DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
    recent_within_seconds: int | None = None,
    limit: int = 20,
    heartbeat_model: type[Any] = AgentHeartbeat,
) -> tuple[list[MachineTransportHealthSummary], int]:
    if device_id:
        summary_map = load_machine_transport_health_map(
            db,
            device_ids=[device_id],
            stale_after_seconds=stale_after_seconds,
            recent_within_seconds=recent_within_seconds,
            heartbeat_model=heartbeat_model,
        )
        summaries = list(summary_map.values())
    else:
        summaries = list(
            load_machine_transport_health_map(
                db,
                stale_after_seconds=stale_after_seconds,
                recent_within_seconds=recent_within_seconds,
                heartbeat_model=heartbeat_model,
            ).values()
        )
    if status:
        summaries = [item for item in summaries if item.status == status]
    summaries.sort(
        key=lambda item: (
            _STATE_SORT_ORDER.get(item.status, 99),
            -item.last_heartbeat_at.timestamp(),
            item.device_id,
        )
    )
    total = len(summaries)
    return summaries[:limit], total


def load_machine_transport_health_map(
    db: Session,
    *,
    device_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    stale_after_seconds: int = DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
    recent_within_seconds: int | None = None,
    heartbeat_model: type[Any] = AgentHeartbeat,
) -> dict[str, MachineTransportHealthSummary]:
    # Heartbeats are append-only server-side writes, so max(id) gives us the
    # newest durable row per device without a timestamp self-join.
    recent_after = None
    normalized_recent_within_seconds = int(recent_within_seconds) if recent_within_seconds is not None else None
    if normalized_recent_within_seconds is not None and normalized_recent_within_seconds > 0:
        recent_after = utc_now() - timedelta(seconds=normalized_recent_within_seconds)
    latest_ids = db.query(func.max(heartbeat_model.id).label("heartbeat_id"))
    normalized_device_ids = sorted({str(device_id).strip() for device_id in device_ids or [] if str(device_id).strip()})
    if normalized_device_ids:
        latest_ids = latest_ids.filter(heartbeat_model.device_id.in_(normalized_device_ids))
    if recent_after is not None:
        latest_ids = latest_ids.filter(heartbeat_model.received_at >= recent_after)
    latest_ids = latest_ids.group_by(heartbeat_model.device_id).subquery()

    rows = db.query(heartbeat_model).join(latest_ids, heartbeat_model.id == latest_ids.c.heartbeat_id).all()
    now = utc_now()
    return {
        row.device_id: build_machine_transport_health_summary(
            row,
            stale_after_seconds=stale_after_seconds,
            now=now,
        )
        for row in rows
    }


def build_machine_transport_health_summary(
    row: Any,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> MachineTransportHealthSummary:
    observed_now = normalize_utc(now) if now is not None else utc_now()
    last_heartbeat_at = normalize_utc(row.received_at) or observed_now
    heartbeat_age_seconds = max(0, int((observed_now - last_heartbeat_at).total_seconds()))

    sample = transport_health_sample_from_heartbeat(row)
    transport = assess_transport_health(sample)

    ship_attempts_1h = sample.ship_attempts_1h
    ship_successes_1h = sample.ship_successes_1h
    ship_success_rate_1h = sample.ship_success_rate_1h
    spool_pending = sample.spool_pending
    spool_dead = sample.spool_dead
    archive_repair = _archive_repair_from_heartbeat(row, spool_pending=spool_pending)
    history_import = _history_import_from_heartbeat(row)
    parse_errors_1h = sample.parse_errors_1h
    consecutive_failures = sample.consecutive_failures
    ship_rate_limited_1h = sample.ship_rate_limited_1h
    ship_server_errors_1h = sample.ship_server_errors_1h
    ship_payload_rejections_1h = sample.ship_payload_rejections_1h
    ship_payload_too_large_1h = sample.ship_payload_too_large_1h
    ship_retryable_client_errors_1h = sample.ship_retryable_client_errors_1h
    ship_connect_errors_1h = sample.ship_connect_errors_1h
    ship_attempts_10m = sample.ship_attempts_10m
    ship_successes_10m = sample.ship_successes_10m
    ship_rate_limited_10m = sample.ship_rate_limited_10m
    ship_server_errors_10m = sample.ship_server_errors_10m
    ship_retryable_client_errors_10m = sample.ship_retryable_client_errors_10m
    ship_connect_errors_10m = sample.ship_connect_errors_10m
    disk_free_bytes = int(row.disk_free_bytes or 0)
    is_offline = sample.is_offline
    is_stale = heartbeat_age_seconds > stale_after_seconds
    reasons = _overlay_archive_repair_reasons(
        _overlay_heartbeat_staleness(
            transport=transport,
            is_stale=is_stale,
        ),
        archive_repair=archive_repair,
    )
    heartbeat_status, heartbeat_status_reason, heartbeat_status_summary = _overlay_heartbeat_status(
        transport=transport,
        is_stale=is_stale,
        heartbeat_age_seconds=heartbeat_age_seconds,
    )
    status, status_reason, status_summary = _overlay_archive_repair_status(
        heartbeat_status,
        heartbeat_status_reason,
        heartbeat_status_summary,
        archive_repair=archive_repair,
    )

    return MachineTransportHealthSummary(
        device_id=row.device_id,
        version=row.version,
        last_heartbeat_at=last_heartbeat_at,
        heartbeat_age_seconds=heartbeat_age_seconds,
        stale_after_seconds=stale_after_seconds,
        is_stale=is_stale,
        status=status,
        status_reason=status_reason,
        status_summary=status_summary,
        reasons=tuple(reasons),
        last_ship_at=normalize_utc(row.last_ship_at),
        last_ship_attempt_at=normalize_utc(row.last_ship_attempt_at),
        last_ship_result=row.last_ship_result,
        last_ship_latency_ms=row.last_ship_latency_ms,
        last_ship_http_status=row.last_ship_http_status,
        last_ship_error_kind=sample.last_ship_error_kind,
        last_ship_error_message=sample.last_ship_error_message,
        ship_attempts_1h=ship_attempts_1h,
        ship_successes_1h=ship_successes_1h,
        ship_success_rate_1h=ship_success_rate_1h,
        ship_rate_limited_1h=ship_rate_limited_1h,
        ship_server_errors_1h=ship_server_errors_1h,
        ship_payload_rejections_1h=ship_payload_rejections_1h,
        ship_payload_too_large_1h=ship_payload_too_large_1h,
        ship_retryable_client_errors_1h=ship_retryable_client_errors_1h,
        ship_connect_errors_1h=ship_connect_errors_1h,
        ship_latency_p50_ms_1h=row.ship_latency_p50_ms_1h,
        ship_latency_p95_ms_1h=row.ship_latency_p95_ms_1h,
        ship_attempts_10m=ship_attempts_10m,
        ship_successes_10m=ship_successes_10m,
        ship_rate_limited_10m=ship_rate_limited_10m,
        ship_server_errors_10m=ship_server_errors_10m,
        ship_retryable_client_errors_10m=ship_retryable_client_errors_10m,
        ship_connect_errors_10m=ship_connect_errors_10m,
        spool_pending=spool_pending,
        spool_dead=spool_dead,
        archive_repair=archive_repair,
        history_import=history_import,
        parse_errors_1h=parse_errors_1h,
        consecutive_failures=consecutive_failures,
        disk_free_bytes=disk_free_bytes,
        is_offline=is_offline,
    )


def _archive_repair_from_heartbeat(row: AgentHeartbeat, *, spool_pending: int) -> dict[str, Any]:
    raw_json = getattr(row, "raw_json", None)
    raw: dict[str, Any] = {}
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, ValueError):
            raw = {}
    archive_backlog = raw.get("archive_backlog")
    if isinstance(archive_backlog, dict):
        return normalize_archive_backlog(archive_backlog, source="heartbeat")
    fallback = default_archive_backlog(source="heartbeat_legacy")
    if spool_pending > 0:
        fallback.update({"state": "pending", "mode": "trickle", "pending_ranges": int(spool_pending)})
    return fallback


def _history_import_from_heartbeat(row: AgentHeartbeat) -> HistoryImportSnapshot:
    raw_json = getattr(row, "raw_json", None)
    if not raw_json:
        return HistoryImportSnapshot.unavailable()
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError):
        return HistoryImportSnapshot.unavailable()
    if not isinstance(raw, dict) or not isinstance(raw.get("history_import"), dict):
        return HistoryImportSnapshot.unavailable()
    try:
        return HistoryImportSnapshot.model_validate(raw["history_import"])
    except ValueError:
        return HistoryImportSnapshot.unavailable()


def _archive_dead_count(archive_repair: dict[str, Any], key: str) -> int:
    value = archive_repair.get(key)
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _archive_has_dead_letters(archive_repair: dict[str, Any]) -> bool:
    return (
        str(archive_repair.get("state") or "").strip() == "dead_lettered"
        or _archive_dead_count(archive_repair, "dead_ranges") > 0
        or _archive_dead_count(archive_repair, "dead_bytes") > 0
    )


def _archive_dead_letter_summary(archive_repair: dict[str, Any]) -> str:
    dead_ranges = _archive_dead_count(archive_repair, "dead_ranges")
    if dead_ranges > 0:
        return f"{dead_ranges} dead-letter archive range(s) need attention."
    dead_bytes = _archive_dead_count(archive_repair, "dead_bytes")
    if dead_bytes > 0:
        return f"{dead_bytes} dead-letter archive byte(s) need attention."
    return "Archive repair has dead-lettered work."


def _overlay_archive_repair_reasons(
    reasons: tuple[str, ...],
    *,
    archive_repair: dict[str, Any],
) -> tuple[str, ...]:
    if not _archive_has_dead_letters(archive_repair) or "archive_dead_lettered" in reasons:
        return reasons
    return (*reasons, "archive_dead_lettered")


def _overlay_archive_repair_status(
    status: str,
    status_reason: str,
    status_summary: str,
    *,
    archive_repair: dict[str, Any],
) -> tuple[str, str, str]:
    if not _archive_has_dead_letters(archive_repair) or status != "healthy":
        return status, status_reason, status_summary
    return (
        "degraded",
        "archive_dead_lettered",
        _archive_dead_letter_summary(archive_repair),
    )


def _overlay_heartbeat_staleness(
    *,
    transport: TransportHealthAssessment,
    is_stale: bool,
) -> tuple[str, ...]:
    reasons = list(transport.reasons)
    if is_stale and "heartbeat_stale" not in reasons:
        reasons.insert(0, "heartbeat_stale")
    return tuple(reasons)


def _overlay_heartbeat_status(
    *,
    transport: TransportHealthAssessment,
    is_stale: bool,
    heartbeat_age_seconds: int,
) -> tuple[str, str, str]:
    # Heartbeat freshness is a hosted-only liveness overlay. Keep continuity
    # failures broken, but let stale heartbeats supersede non-broken in-band
    # transport states such as "reported_offline".
    if is_stale and transport.status != "broken":
        return (
            "offline",
            "heartbeat_stale",
            f"Last heartbeat {heartbeat_age_seconds}s ago.",
        )
    return (
        transport.status,
        transport.status_reason,
        transport.status_summary,
    )
