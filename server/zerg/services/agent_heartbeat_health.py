"""Machine-facing transport health summaries derived from latest heartbeats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.models.agents import AgentHeartbeat
from zerg.utils.time import normalize_utc
from zerg.utils.time import utc_now

DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS = 15 * 60

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
    spool_pending: int
    spool_dead: int
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
    limit: int = 20,
) -> tuple[list[MachineTransportHealthSummary], int]:
    if device_id:
        summary_map = load_machine_transport_health_map(
            db,
            device_ids=[device_id],
            stale_after_seconds=stale_after_seconds,
        )
        summaries = list(summary_map.values())
    else:
        summaries = list(
            load_machine_transport_health_map(
                db,
                stale_after_seconds=stale_after_seconds,
            ).values()
        )
    if status:
        summaries = [item for item in summaries if item.status == status]
    summaries.sort(key=lambda item: (_STATE_SORT_ORDER.get(item.status, 99), -item.last_heartbeat_at.timestamp(), item.device_id))
    total = len(summaries)
    return summaries[:limit], total


def load_machine_transport_health_map(
    db: Session,
    *,
    device_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    stale_after_seconds: int = DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
) -> dict[str, MachineTransportHealthSummary]:
    # Heartbeats are append-only server-side writes, so max(id) gives us the
    # newest durable row per device without a timestamp self-join.
    latest_ids = db.query(func.max(AgentHeartbeat.id).label("heartbeat_id"))
    normalized_device_ids = sorted({str(device_id).strip() for device_id in device_ids or [] if str(device_id).strip()})
    if normalized_device_ids:
        latest_ids = latest_ids.filter(AgentHeartbeat.device_id.in_(normalized_device_ids))
    latest_ids = latest_ids.group_by(AgentHeartbeat.device_id).subquery()

    rows = db.query(AgentHeartbeat).join(latest_ids, AgentHeartbeat.id == latest_ids.c.heartbeat_id).all()
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
    row: AgentHeartbeat,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> MachineTransportHealthSummary:
    observed_now = normalize_utc(now) if now is not None else utc_now()
    last_heartbeat_at = normalize_utc(row.received_at) or observed_now
    heartbeat_age_seconds = max(0, int((observed_now - last_heartbeat_at).total_seconds()))

    ship_attempts_1h = int(row.ship_attempts_1h or 0)
    ship_successes_1h = int(row.ship_successes_1h or 0)
    ship_success_rate_1h = None
    if ship_attempts_1h > 0:
        ship_success_rate_1h = round(ship_successes_1h / ship_attempts_1h, 4)

    spool_pending = int(row.spool_pending or 0)
    spool_dead = int(row.spool_dead or 0)
    parse_errors_1h = int(row.parse_errors_1h or 0)
    consecutive_failures = int(row.consecutive_failures or 0)
    ship_rate_limited_1h = int(row.ship_rate_limited_1h or 0)
    ship_server_errors_1h = int(row.ship_server_errors_1h or 0)
    ship_payload_rejections_1h = int(row.ship_payload_rejections_1h or 0)
    ship_payload_too_large_1h = int(row.ship_payload_too_large_1h or 0)
    ship_retryable_client_errors_1h = int(row.ship_retryable_client_errors_1h or 0)
    ship_connect_errors_1h = int(row.ship_connect_errors_1h or 0)
    disk_free_bytes = int(row.disk_free_bytes or 0)
    is_offline = bool(row.is_offline)
    is_stale = heartbeat_age_seconds > stale_after_seconds

    reasons: list[str] = []
    if is_stale:
        reasons.append("heartbeat_stale")
    if is_offline:
        reasons.append("reported_offline")
    if spool_dead > 0:
        reasons.append("spool_dead")
    if ship_payload_rejections_1h > 0:
        reasons.append("payload_rejected")
    if ship_payload_too_large_1h > 0:
        reasons.append("payload_too_large")
    if parse_errors_1h > 0:
        reasons.append("parse_errors")
    if consecutive_failures > 0:
        reasons.append("consecutive_failures")
    if ship_connect_errors_1h > 0:
        reasons.append("connect_errors")
    if ship_server_errors_1h > 0:
        reasons.append("server_errors")
    if ship_rate_limited_1h > 0:
        reasons.append("rate_limited")
    if ship_retryable_client_errors_1h > 0:
        reasons.append("retryable_client_errors")
    if spool_pending > 0:
        reasons.append("spool_pending")

    # Known continuity-loss conditions win over liveness because the operator
    # still needs to repair them after the machine comes back.
    if spool_dead > 0:
        status = "broken"
        status_reason = "spool_dead"
        status_summary = f"{spool_dead} dead-letter range(s) need repair."
    elif ship_payload_rejections_1h > 0:
        status = "broken"
        status_reason = "payload_rejected"
        status_summary = f"{ship_payload_rejections_1h} ship payload rejection(s) in the last hour."
    elif ship_payload_too_large_1h > 0:
        status = "broken"
        status_reason = "payload_too_large"
        status_summary = f"{ship_payload_too_large_1h} ship payload too-large rejection(s) in the last hour."
    elif is_stale:
        status = "offline"
        status_reason = "heartbeat_stale"
        status_summary = f"Last heartbeat {heartbeat_age_seconds}s ago."
    elif is_offline:
        status = "offline"
        status_reason = "reported_offline"
        status_summary = "Engine reported offline."
    elif parse_errors_1h > 0:
        status = "degraded"
        status_reason = "parse_errors"
        status_summary = f"{parse_errors_1h} parse error(s) in the last hour."
    elif consecutive_failures > 0:
        status = "degraded"
        status_reason = "consecutive_failures"
        status_summary = f"{consecutive_failures} consecutive ship failure(s)."
    elif ship_connect_errors_1h > 0:
        status = "degraded"
        status_reason = "connect_errors"
        status_summary = f"{ship_connect_errors_1h} ship connect error(s) in the last hour."
    elif ship_server_errors_1h > 0:
        status = "degraded"
        status_reason = "server_errors"
        status_summary = f"{ship_server_errors_1h} ship server error(s) in the last hour."
    elif ship_rate_limited_1h > 0:
        status = "degraded"
        status_reason = "rate_limited"
        status_summary = f"{ship_rate_limited_1h} rate-limit response(s) in the last hour."
    elif ship_retryable_client_errors_1h > 0:
        status = "degraded"
        status_reason = "retryable_client_errors"
        status_summary = f"{ship_retryable_client_errors_1h} retryable client error(s) in the last hour."
    elif spool_pending > 0:
        status = "degraded"
        status_reason = "spool_pending"
        status_summary = f"{spool_pending} pending spool item(s)."
    else:
        status = "healthy"
        status_reason = "healthy"
        status_summary = "Shipping healthy."

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
        spool_pending=spool_pending,
        spool_dead=spool_dead,
        parse_errors_1h=parse_errors_1h,
        consecutive_failures=consecutive_failures,
        disk_free_bytes=disk_free_bytes,
        is_offline=is_offline,
    )
