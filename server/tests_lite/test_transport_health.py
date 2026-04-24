# ruff: noqa: I001

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.models.agents import AgentHeartbeat
from zerg.services.transport_health import assess_transport_health
from zerg.services.transport_health import transport_health_sample_from_engine_status_payload
from zerg.services.transport_health import transport_health_sample_from_heartbeat


def test_transport_health_builders_keep_heartbeat_and_local_payload_in_sync():
    row = AgentHeartbeat(
        device_id="cinder",
        spool_pending=0,
        spool_dead=0,
        parse_errors_1h=0,
        consecutive_failures=0,
        ship_attempts_1h=20,
        ship_successes_1h=18,
        ship_connect_errors_1h=2,
        is_offline=0,
    )
    payload = {
        "spool_pending_count": 0,
        "spool_dead_count": 0,
        "parse_error_count_1h": 0,
        "consecutive_ship_failures": 0,
        "ship_attempts_1h": 20,
        "ship_successes_1h": 18,
        "ship_connect_errors_1h": 2,
        "is_offline": False,
    }

    heartbeat_sample = transport_health_sample_from_heartbeat(row)
    local_sample = transport_health_sample_from_engine_status_payload(payload)

    assert heartbeat_sample == local_sample

    heartbeat_assessment = assess_transport_health(heartbeat_sample)
    local_assessment = assess_transport_health(local_sample)

    assert heartbeat_assessment == local_assessment
    assert heartbeat_assessment.status == "degraded"
    assert heartbeat_assessment.status_reason == "connect_errors"
    assert heartbeat_assessment.status_summary == "2 ship connect error(s) in the last hour."
    assert heartbeat_assessment.reasons == ("connect_errors",)


def test_transport_health_keeps_single_transient_connect_error_healthy():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 65,
            "ship_successes_1h": 64,
            "ship_connect_errors_1h": 1,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()
