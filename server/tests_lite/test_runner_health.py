from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from zerg.services.runner_health import assess_runner_health
from zerg.utils.time import utc_now_naive


def _runner(**overrides):
    now = utc_now_naive()
    base = {
        "id": 1,
        "owner_id": 1,
        "name": "zerg",
        "status": "online",
        "last_seen_at": now,
        "capabilities": ["exec.full"],
        "runner_metadata": {
            "install_mode": "server",
            "auto_update_policy": "notify",
            "install_layout_version": 1,
            "runner_version": "0.1.0",
            "capabilities": ["exec.full"],
            "heartbeat_interval_ms": 30_000,
        },
        "created_at": now,
        "updated_at": now,
        "labels": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_assess_runner_health_marks_fresh_runner_online():
    runner = _runner()
    health = assess_runner_health(runner, latest_runner_version="0.1.3")

    assert health.effective_status == "online"
    assert health.status_reason == "fresh_heartbeat"
    assert health.availability_policy == "always_on"
    assert health.install_mode == "server"
    assert health.auto_update_policy == "notify"
    assert health.install_layout_version == 1
    assert health.managed_install_ready is True
    assert health.version_status == "outdated"
    assert health.capabilities_match is True


def test_assess_runner_health_marks_stale_runner_offline():
    now = utc_now_naive()
    runner = _runner(last_seen_at=now - timedelta(minutes=5))
    health = assess_runner_health(runner, now=now, latest_runner_version="0.1.3")

    assert health.effective_status == "offline"
    assert health.status_reason == "stale_heartbeat"
    assert health.availability_policy == "always_on"
    assert health.is_stale is True
    assert health.last_seen_age_seconds == 300


def test_assess_runner_health_marks_missing_connection_offline_even_when_heartbeat_is_fresh():
    runner = _runner()
    health = assess_runner_health(runner, latest_runner_version="0.1.3", is_connected=False)

    assert health.effective_status == "offline"
    assert health.status_reason == "disconnected_recently"
    assert health.is_connected is False


def test_assess_runner_health_marks_never_connected_runner_offline():
    runner = _runner(status="offline", last_seen_at=None, runner_metadata=None)
    health = assess_runner_health(runner, latest_runner_version="0.1.3")

    assert health.effective_status == "offline"
    assert health.status_reason == "never_connected"
    assert health.availability_policy == "always_on"
    assert health.capabilities_match is None
    assert health.managed_install_ready is False
    assert health.version_status == "unknown"


def test_assess_runner_health_marks_revoked_runner_revoked():
    runner = _runner(status="revoked", last_seen_at=None)
    health = assess_runner_health(runner, latest_runner_version="0.1.3")

    assert health.effective_status == "revoked"
    assert health.status_reason == "revoked"


def test_assess_runner_health_infers_on_demand_for_desktop_runner():
    now = utc_now_naive()
    runner = _runner(
        last_seen_at=now - timedelta(minutes=5),
        runner_metadata={
            "install_mode": "desktop",
            "auto_update_policy": "notify",
            "install_layout_version": 1,
            "runner_version": "0.1.0",
            "capabilities": ["exec.full"],
            "heartbeat_interval_ms": 30_000,
        },
    )
    health = assess_runner_health(runner, now=now, latest_runner_version="0.1.3")

    assert health.effective_status == "offline"
    assert health.availability_policy == "on_demand"
    assert "configured as on-demand" in health.status_summary


def test_assess_runner_health_respects_explicit_availability_policy_override():
    now = utc_now_naive()
    runner = _runner(
        availability_policy="ephemeral",
        last_seen_at=now - timedelta(minutes=5),
    )
    health = assess_runner_health(runner, now=now, latest_runner_version="0.1.3")

    assert health.availability_policy == "ephemeral"
    assert "ephemeral" in health.status_summary
