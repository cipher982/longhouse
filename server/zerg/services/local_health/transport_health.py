from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zerg.services.managed_provider_contracts import machine_control_operations_by_provider
from zerg.services.transport_health import TransportHealthAssessment
from zerg.services.transport_health import TransportHealthSample
from zerg.services.transport_health import assess_transport_health
from zerg.services.transport_health import transport_health_sample_from_engine_status_payload


def _collect_transport_health(
    engine_status: dict[str, Any],
) -> tuple[TransportHealthSample | None, TransportHealthAssessment | None]:
    if not bool(engine_status.get("exists")):
        return None, None
    if engine_status.get("error"):
        return None, None
    raw_payload = engine_status.get("payload")
    if not isinstance(raw_payload, Mapping):
        return None, None
    sample = transport_health_sample_from_engine_status_payload(raw_payload)
    return sample, assess_transport_health(sample)


def _serialize_transport_health(
    *,
    sample: TransportHealthSample | None,
    assessment: TransportHealthAssessment | None,
) -> dict[str, Any] | None:
    if sample is None or assessment is None:
        return None
    return {
        "source": "engine_status",
        "status": assessment.status,
        "status_reason": assessment.status_reason,
        "status_summary": assessment.status_summary,
        "reasons": list(assessment.reasons),
        "ship_attempts_1h": sample.ship_attempts_1h,
        "ship_successes_1h": sample.ship_successes_1h,
        "ship_success_rate_1h": sample.ship_success_rate_1h,
        "ship_rate_limited_1h": sample.ship_rate_limited_1h,
        "ship_server_errors_1h": sample.ship_server_errors_1h,
        "ship_payload_rejections_1h": sample.ship_payload_rejections_1h,
        "ship_payload_too_large_1h": sample.ship_payload_too_large_1h,
        "ship_retryable_client_errors_1h": sample.ship_retryable_client_errors_1h,
        "ship_connect_errors_1h": sample.ship_connect_errors_1h,
        "ship_attempts_10m": sample.ship_attempts_10m,
        "ship_successes_10m": sample.ship_successes_10m,
        "ship_rate_limited_10m": sample.ship_rate_limited_10m,
        "ship_server_errors_10m": sample.ship_server_errors_10m,
        "ship_retryable_client_errors_10m": sample.ship_retryable_client_errors_10m,
        "ship_connect_errors_10m": sample.ship_connect_errors_10m,
        "last_ship_result": sample.last_ship_result,
        "last_ship_http_status": sample.last_ship_http_status,
        "last_ship_error_kind": sample.last_ship_error_kind,
        "last_ship_error_message": sample.last_ship_error_message,
        "spool_pending": sample.spool_pending,
        "spool_dead": sample.spool_dead,
        "parse_errors_1h": sample.parse_errors_1h,
        "consecutive_failures": sample.consecutive_failures,
        "is_offline": sample.is_offline,
    }


def _collect_control_channel_health(engine_status: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(engine_status.get("exists")) or engine_status.get("error"):
        return None
    raw_payload = engine_status.get("payload")
    if not isinstance(raw_payload, Mapping):
        return None
    raw_control = raw_payload.get("control_channel")
    if not isinstance(raw_control, Mapping):
        return None

    supports = [str(item) for item in list(raw_control.get("supports") or []) if str(item).strip()]
    status = str(raw_control.get("status") or "disabled").strip() or "disabled"
    connected = status == "connected"
    operations_by_provider = machine_control_operations_by_provider(supports, connected=connected)
    control_operations_by_provider = {}
    for provider, operations in sorted(operations_by_provider.items()):
        control_operations_by_provider[provider] = list(operations)
    console_ready_providers = sorted(provider for provider, operations in operations_by_provider.items() if "turn_start" in operations)
    console_blocked_by = None
    if not console_ready_providers:
        console_blocked_by = "no_console_support" if connected else "control_down"

    return {
        "source": "engine_status",
        "enabled": bool(raw_control.get("enabled")),
        "status": status,
        "ws_url": raw_control.get("ws_url"),
        "last_connected_at": raw_control.get("last_connected_at"),
        "last_disconnected_at": raw_control.get("last_disconnected_at"),
        "last_error_code": raw_control.get("last_error_code"),
        "last_error_message": raw_control.get("last_error_message"),
        "reconnect_backoff_seconds": raw_control.get("reconnect_backoff_seconds"),
        "supports": supports,
        "control_operations_by_provider": control_operations_by_provider,
        "console_ready_providers": console_ready_providers,
        "console_blocked_by": console_blocked_by,
    }


__all__ = ["_collect_transport_health", "_serialize_transport_health", "_collect_control_channel_health"]
