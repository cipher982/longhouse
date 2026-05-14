# ruff: noqa: I001

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

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
        ship_successes_1h=15,
        ship_connect_errors_1h=5,
        is_offline=0,
    )
    payload = {
        "spool_pending_count": 0,
        "spool_dead_count": 0,
        "parse_error_count_1h": 0,
        "consecutive_ship_failures": 0,
        "ship_attempts_1h": 20,
        "ship_successes_1h": 15,
        "ship_connect_errors_1h": 5,
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
    assert heartbeat_assessment.status_summary == "5 ship connect error(s) in the last hour."
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


def test_transport_health_keeps_recovered_transient_connect_errors_healthy():
    sample = transport_health_sample_from_engine_status_payload(
        {
            "ship_attempts_1h": 14,
            "ship_successes_1h": 12,
            "ship_connect_errors_1h": 2,
            "last_ship_result": "ok",
            "consecutive_ship_failures": 0,
            "spool_pending_count": 0,
            "spool_dead_count": 0,
        }
    )

    assessment = assess_transport_health(sample)

    assert assessment.status == "healthy"
    assert assessment.status_reason == "healthy"
    assert assessment.status_summary == "Shipping healthy."
    assert assessment.reasons == ()


def test_transport_health_surfaces_last_transport_error_detail():
    payload = {
        "ship_attempts_1h": 20,
        "ship_successes_1h": 18,
        "ship_connect_errors_1h": 2,
        "last_ship_result": "connect_error",
        "last_ship_error_kind": "timeout",
        "last_ship_error_message": "request timed out after 60s",
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
    assert assessment.status_summary == "2 ship connect error(s) in the last hour. Last error: timeout."


def test_local_health_cli_does_not_require_database_url(tmp_path):
    repo_server_dir = Path(__file__).resolve().parent.parent
    build_identity_path = repo_server_dir / "zerg" / "build_identity.json"
    build_identity_existed = build_identity_path.exists()
    if not build_identity_existed:
        build_identity_path.write_text(
            json.dumps(
                {
                    "version": "0.0.0",
                    "commit": "0" * 40,
                    "commit_short": "00000000",
                    "dirty": False,
                    "built_at": "2026-04-24T00:00:00Z",
                    "channel": "dev",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env.pop("TESTING", None)
    env["HOME"] = str(tmp_path)

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "zerg.cli.main", "local-health", "--fast", "--json"],
            capture_output=True,
            text=True,
            cwd=repo_server_dir,
            env=env,
            check=False,
        )
    finally:
        if not build_identity_existed and build_identity_path.exists():
            build_identity_path.unlink()

    assert completed.returncode == 0, (
        "local-health CLI should not require DATABASE_URL just to read local machine state.\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == 1
    assert "health_state" in payload
