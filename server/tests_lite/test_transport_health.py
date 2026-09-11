# ruff: noqa: I001

from __future__ import annotations

import json
import os
from dataclasses import replace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.models.agents import AgentHeartbeat
from zerg.services.transport_health import assess_transport_health
from zerg.services.transport_health import transport_health_sample_from_engine_status_payload
from zerg.services.transport_health import transport_health_sample_from_heartbeat


def _healthy_shipping_progress() -> dict[str, object]:
    return {
        "pending_work": False,
        "stalled": False,
        "seconds_without_progress": 0,
        "observed_at": "2026-01-01T00:00:00Z",
    }


def test_transport_health_builders_keep_heartbeat_and_local_payload_in_sync():
    row = AgentHeartbeat(
        device_id="cinder",
        spool_pending=0,
        spool_dead=0,
        parse_errors_1h=0,
        ship_attempts_1h=20,
        ship_successes_1h=15,
        ship_connect_errors_1h=5,
        is_offline=0,
        raw_json=json.dumps(
            {
                "ship_attempts_10m": 5,
                "ship_connect_errors_10m": 5,
                "last_ship_result": "connect_error",
                "shipping_progress": _healthy_shipping_progress(),
            }
        ),
    )
    payload = {
        "spool_pending_count": 0,
        "spool_dead_count": 0,
        "parse_error_count_1h": 0,
        "ship_attempts_1h": 20,
        "ship_successes_1h": 15,
        "ship_connect_errors_1h": 5,
        "ship_attempts_10m": 5,
        "ship_connect_errors_10m": 5,
        "last_ship_result": "connect_error",
        "shipping_progress": _healthy_shipping_progress(),
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
    assert heartbeat_assessment.status_summary == "5 ship connect error(s) in the last 10 minutes."
    assert heartbeat_assessment.reasons == ("connect_errors",)


def test_transport_health_marks_missing_engine_transport_evidence_unknown():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "spool_pending_count": 0,
            "spool_dead_count": 0,
            "is_offline": False,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "unknown"
    assert assessment.status_reason == "transport_unavailable"
    assert assessment.reasons == ("transport_unavailable",)


def test_transport_health_fails_unknown_shipping_progress_closed():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 1,
            "ship_successes_1h": 1,
            "shipping_progress": {"pending_work": True},
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "unknown"
    assert assessment.status_reason == "transport_unavailable"
    assert assessment.reasons == ("transport_unavailable",)


def test_transport_health_fails_unknown_when_progress_is_omitted_with_pending_work():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 12,
            "ship_successes_1h": 12,
            "spool_pending_count": 1,
            "last_ship_result": "ok",
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "unknown"
    assert assessment.status_reason == "transport_unavailable"
    assert assessment.reasons == ("transport_unavailable",)


def test_transport_health_fails_unknown_when_heartbeat_carries_old_progress():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 1,
            "ship_successes_1h": 1,
            "shipping_progress": {
                "pending_work": False,
                "stalled": False,
                "seconds_without_progress": 0,
                "observed_at": (now - timedelta(seconds=121)).isoformat(),
            },
        }
    )
    stale = replace(sample, observed_at=now)

    assessment = assess_transport_health(stale)

    assert assessment.status == "unknown"
    assert assessment.status_reason == "transport_unavailable"


def test_transport_health_uses_active_window_to_clear_recovered_hourly_burst():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 32,
            "ship_successes_1h": 20,
            "ship_connect_errors_1h": 12,
            "ship_attempts_10m": 4,
            "ship_successes_10m": 4,
            "ship_connect_errors_10m": 0,
            "last_ship_result": "ok",
            "shipping_progress": _healthy_shipping_progress(),
            "spool_pending_count": 0,
            "spool_dead_count": 0,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()


def test_transport_health_keeps_recovered_server_error_burst_healthy():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_10m": 674,
            "ship_successes_10m": 473,
            "ship_server_errors_10m": 201,
            "last_ship_result": "ok",
            "shipping_progress": _healthy_shipping_progress(),
            "spool_pending_count": 2760,
            "spool_dead_count": 0,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()


def test_transport_health_degrades_for_active_connect_burst():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 40,
            "ship_successes_1h": 34,
            "ship_connect_errors_1h": 6,
            "ship_attempts_10m": 8,
            "ship_successes_10m": 5,
            "ship_connect_errors_10m": 3,
            "last_ship_result": "ok",
            "shipping_progress": _healthy_shipping_progress(),
            "spool_pending_count": 0,
            "spool_dead_count": 0,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "degraded"
    assert assessment.status_reason == "connect_errors"
    assert assessment.status_summary == "3 ship connect error(s) in the last 10 minutes."
    assert assessment.reasons == ("connect_errors",)


def test_transport_health_keeps_single_transient_connect_error_healthy():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 65,
            "ship_successes_1h": 64,
            "ship_connect_errors_1h": 1,
            "shipping_progress": _healthy_shipping_progress(),
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()


def test_transport_health_keeps_single_current_connect_error_healthy():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 12,
            "ship_successes_1h": 11,
            "ship_connect_errors_1h": 1,
            "last_ship_result": "connect_error",
            "shipping_progress": _healthy_shipping_progress(),
            "spool_pending_count": 1,
            "spool_dead_count": 0,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()


def test_transport_health_degrades_for_repeated_current_connect_errors():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 12,
            "ship_successes_1h": 10,
            "ship_connect_errors_1h": 2,
            "ship_attempts_10m": 2,
            "ship_connect_errors_10m": 2,
            "last_ship_result": "connect_error",
            "shipping_progress": _healthy_shipping_progress(),
            "spool_pending_count": 1,
            "spool_dead_count": 0,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "degraded"
    assert assessment.status_reason == "connect_errors"
    assert assessment.reasons == ("connect_errors",)


def test_transport_health_keeps_recovered_transient_connect_errors_healthy():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 14,
            "ship_successes_1h": 12,
            "ship_connect_errors_1h": 2,
            "last_ship_result": "ok",
            "shipping_progress": _healthy_shipping_progress(),
            "spool_pending_count": 0,
            "spool_dead_count": 0,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()


def test_transport_health_keeps_small_spool_retry_healthy():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 12,
            "ship_successes_1h": 11,
            "spool_pending_count": 1,
            "spool_dead_count": 0,
            "last_ship_result": "ok",
            "shipping_progress": _healthy_shipping_progress(),
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.reasons == ()


def test_transport_health_keeps_spool_backlog_out_of_live_transport_status():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 12,
            "ship_successes_1h": 11,
            "spool_pending_count": 5,
            "spool_dead_count": 0,
            "last_ship_result": "ok",
            "shipping_progress": _healthy_shipping_progress(),
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()


def test_transport_health_treats_dead_archive_ranges_as_degraded_attention():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 552,
            "ship_successes_1h": 552,
            "spool_pending_count": 0,
            "spool_dead_count": 7,
            "last_ship_result": "ok",
            "shipping_progress": _healthy_shipping_progress(),
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "degraded"
    assert assessment.status_reason == "spool_dead"
    assert assessment.status_summary == "7 dead-letter archive range(s) need attention."
    assert assessment.reasons == ("spool_dead",)


def test_transport_health_keeps_payload_rejection_broken_above_dead_ranges():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 20,
            "ship_successes_1h": 19,
            "ship_payload_rejections_1h": 1,
            "spool_pending_count": 0,
            "spool_dead_count": 7,
            "last_ship_result": "payload_rejected",
            "shipping_progress": _healthy_shipping_progress(),
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "broken"
    assert assessment.status_reason == "payload_rejected"
    assert assessment.reasons == ("spool_dead", "payload_rejected")


def test_transport_health_surfaces_last_transport_error_detail():
    payload = {
        "ship_attempts_1h": 20,
        "ship_successes_1h": 18,
        "ship_connect_errors_1h": 2,
        "ship_attempts_10m": 2,
        "ship_connect_errors_10m": 2,
        "last_ship_result": "connect_error",
        "last_ship_error_kind": "timeout",
        "last_ship_error_message": "request timed out after 60s",
        "shipping_progress": _healthy_shipping_progress(),
    }
    row = AgentHeartbeat(
        device_id="cinder",
        ship_attempts_1h=20,
        ship_successes_1h=18,
        ship_connect_errors_1h=2,
        last_ship_result="connect_error",
        raw_json=json.dumps(payload),
    )

    local_sample = transport_health_sample_from_engine_status_payload(payload)
    heartbeat_sample = transport_health_sample_from_heartbeat(row)

    assert local_sample == heartbeat_sample
    assert local_sample.last_ship_error_kind == "timeout"
    assert local_sample.last_ship_error_message == "request timed out after 60s"

    assessment = assess_transport_health(local_sample)

    assert assessment.status == "degraded"
    assert assessment.status_reason == "connect_errors"
    assert assessment.status_summary == "2 ship connect error(s) in the last 10 minutes. Last error: timeout."


def test_pending_stalled_progress_is_degraded_not_healthy():
    """The owning daemon, not a consumer wall clock, reports a stalled queue.

    The last recorded result remains ``ok`` and the 1h window can still show
    successful attempts. The daemon's monotonic observation is the only stall
    authority.
    """

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    sample = transport_health_sample_from_engine_status_payload(
        {
            "last_ship_result": "ok",
            "ship_attempts_1h": 41,
            "ship_successes_1h": 41,
            "shipping_progress": {
                "pending_work": True,
                "stalled": True,
                "seconds_without_progress": 60,
                "observed_at": now.isoformat(),
            },
        }
    )
    stalled = replace(sample, last_ship_at=now - timedelta(hours=33), observed_at=now)

    assessment = assess_transport_health(stalled)

    assert assessment.status == "degraded"
    assert assessment.status_reason == "ship_stalled"
    assert "ship_stalled" in assessment.reasons


def test_a_recent_ship_and_an_unknown_ship_time_both_stay_healthy():
    """Quiet is not the same as stalled, and unknown is not the same as stale.

    A machine between sessions ships nothing for minutes at a time, and a
    heartbeat that carries no ``last_ship_at`` cannot support any claim about
    elapsed time. Neither may be reported as an outage.
    """

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    base = transport_health_sample_from_engine_status_payload(
        {
            "last_ship_result": "ok",
            "shipping_progress": {
                "pending_work": False,
                "stalled": False,
                "seconds_without_progress": 0,
                "observed_at": now.isoformat(),
            },
        }
    )

    recent = replace(base, last_ship_at=now - timedelta(minutes=5), observed_at=now)
    assert assess_transport_health(recent).status == "healthy"

    idle_with_old_ship = replace(base, last_ship_at=now - timedelta(hours=33), observed_at=now)
    assert assess_transport_health(idle_with_old_ship).status == "healthy"

    assert base.seconds_since_last_ship is None
    assert assess_transport_health(base).status == "healthy"


def test_an_offline_machine_is_described_by_being_offline():
    """Offline already explains the silence; stalled would only add noise."""

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    sample = transport_health_sample_from_engine_status_payload({"is_offline": True})
    offline = replace(sample, is_offline=True, last_ship_at=now - timedelta(hours=33), observed_at=now)

    assert "ship_stalled" not in assess_transport_health(offline).reasons
