"""Tests for the retained machine observability route."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401, F811
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401, F811

OWNER_EMAIL = "owner@observability.test"


def _apply_catalog_heartbeat(
    live_catalog,  # noqa: F811
    *,
    device_id: str,
    received_at: datetime,
    **overrides,
) -> None:
    """Apply one heartbeat stamp directly through catalogd for route coverage."""

    heartbeat = {
        "device_id": device_id,
        "received_at": received_at.isoformat(),
        "version": "0.6.0",
        "last_ship_at": None,
        "last_ship_attempt_at": None,
        "last_ship_result": None,
        "last_ship_latency_ms": None,
        "last_ship_http_status": None,
        "spool_pending": 0,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "ship_attempts_1h": 0,
        "ship_successes_1h": 0,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "ship_latency_p50_ms_1h": None,
        "ship_latency_p95_ms_1h": None,
        "disk_free_bytes": 1_000,
        "is_offline": 0,
        "raw_json": None,
        "sessions_digest": None,
        "sessions_sequence": None,
    }
    heartbeat.update(overrides)
    raw_payload = json.loads(heartbeat["raw_json"]) if heartbeat["raw_json"] is not None else {}
    raw_payload.setdefault(
        "shipping_progress",
        {
            "pending_work": False,
            "stalled": False,
            "seconds_without_progress": 0,
            "observed_at": received_at.isoformat(),
        },
    )
    heartbeat["raw_json"] = json.dumps(raw_payload)
    live_catalog.rpc(
        "machine.heartbeat.apply.v2",
        {
            "heartbeat": heartbeat,
            "managed_leases": [],
            "managed_leases_present": False,
            "owner_id": None,
        },
    )


def test_machine_health_route_reads_the_live_catalog(live_catalog, live_catalog_client):  # noqa: F811
    """`/observability/machines/health` is served from catalogd heartbeat stamps."""

    owner_id = live_catalog.create_user(OWNER_EMAIL)
    live_catalog_client.cookies.set("longhouse_session", live_catalog.browser_cookie(owner_id=owner_id, email=OWNER_EMAIL))
    now = datetime.now(timezone.utc)
    for device_id in ("broken-machine", "healthy-machine", "ancient-machine"):
        live_catalog.create_device_token(owner_id=owner_id, device_id=device_id)

    _apply_catalog_heartbeat(
        live_catalog,
        device_id="broken-machine",
        received_at=now - timedelta(minutes=2),
        spool_dead=1,
        ship_attempts_1h=4,
        ship_successes_1h=2,
    )
    _apply_catalog_heartbeat(
        live_catalog,
        device_id="healthy-machine",
        received_at=now - timedelta(minutes=1),
        ship_attempts_1h=4,
        ship_successes_1h=4,
    )
    _apply_catalog_heartbeat(
        live_catalog,
        device_id="ancient-machine",
        received_at=now - timedelta(days=14),
        ship_attempts_1h=4,
        ship_successes_1h=4,
    )

    degraded = live_catalog_client.get(
        "/observability/machines/health?status=degraded&stale_after_seconds=3600",
    )
    assert degraded.status_code == 200, degraded.text
    degraded_payload = degraded.json()
    assert degraded_payload["total"] == 1
    assert degraded_payload["machines"][0]["device_id"] == "broken-machine"

    recent_default = live_catalog_client.get(
        "/observability/machines/health?stale_after_seconds=3600",
    )
    assert recent_default.status_code == 200, recent_default.text
    recent_default_payload = recent_default.json()
    assert recent_default_payload["total"] == 2
    assert {machine["device_id"] for machine in recent_default_payload["machines"]} == {
        "broken-machine",
        "healthy-machine",
    }

    widened = live_catalog_client.get(
        "/observability/machines/health?stale_after_seconds=3600&recent_within_hours=720",
    )
    assert widened.status_code == 200, widened.text
    widened_payload = widened.json()
    assert widened_payload["total"] == 3
    assert "ancient-machine" in {machine["device_id"] for machine in widened_payload["machines"]}
