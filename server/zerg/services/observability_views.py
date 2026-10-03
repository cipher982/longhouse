"""Shared observability view builders for machine and browser routes."""

from __future__ import annotations

from zerg.schemas.observability import MachineHealthItemResponse
from zerg.schemas.observability import MachineHealthListResponse
from zerg.services.agent_heartbeat_health import MachineTransportHealthSummary


def build_machine_health_item_response(item: MachineTransportHealthSummary) -> MachineHealthItemResponse:
    return MachineHealthItemResponse(
        device_id=item.device_id,
        version=item.version,
        last_heartbeat_at=item.last_heartbeat_at,
        heartbeat_age_seconds=item.heartbeat_age_seconds,
        stale_after_seconds=item.stale_after_seconds,
        is_stale=item.is_stale,
        status=item.status,  # type: ignore[arg-type]
        status_reason=item.status_reason,
        status_summary=item.status_summary,
        reasons=list(item.reasons),
        suggested_action_ids=list(item.suggested_action_ids),
        last_ship_at=item.last_ship_at,
        last_ship_attempt_at=item.last_ship_attempt_at,
        last_ship_result=item.last_ship_result,
        last_ship_latency_ms=item.last_ship_latency_ms,
        last_ship_http_status=item.last_ship_http_status,
        last_ship_error_kind=item.last_ship_error_kind,
        last_ship_error_message=item.last_ship_error_message,
        ship_attempts_1h=item.ship_attempts_1h,
        ship_successes_1h=item.ship_successes_1h,
        ship_success_rate_1h=item.ship_success_rate_1h,
        ship_rate_limited_1h=item.ship_rate_limited_1h,
        ship_server_errors_1h=item.ship_server_errors_1h,
        ship_payload_rejections_1h=item.ship_payload_rejections_1h,
        ship_payload_too_large_1h=item.ship_payload_too_large_1h,
        ship_retryable_client_errors_1h=item.ship_retryable_client_errors_1h,
        ship_connect_errors_1h=item.ship_connect_errors_1h,
        ship_latency_p50_ms_1h=item.ship_latency_p50_ms_1h,
        ship_latency_p95_ms_1h=item.ship_latency_p95_ms_1h,
        ship_attempts_10m=item.ship_attempts_10m,
        ship_successes_10m=item.ship_successes_10m,
        ship_rate_limited_10m=item.ship_rate_limited_10m,
        ship_server_errors_10m=item.ship_server_errors_10m,
        ship_retryable_client_errors_10m=item.ship_retryable_client_errors_10m,
        ship_connect_errors_10m=item.ship_connect_errors_10m,
        spool_pending=item.spool_pending,
        spool_dead=item.spool_dead,
        archive_repair=item.archive_repair,
        history_import=item.history_import,
        parse_errors_1h=item.parse_errors_1h,
        disk_free_bytes=item.disk_free_bytes,
        is_offline=item.is_offline,
    )


def build_machine_health_list_response(
    summaries: list[MachineTransportHealthSummary],
    *,
    total: int,
) -> MachineHealthListResponse:
    return MachineHealthListResponse(
        machines=[build_machine_health_item_response(item) for item in summaries],
        total=total,
    )
