from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zerg.cli.main import app
from zerg.services.archive_backlog import collect_archive_backlog
from zerg.services.archive_backlog import write_archive_control
from zerg.services.longhouse_paths import get_agent_status_path


def test_archive_status_prefers_engine_status_and_includes_shipper_diagnostics(tmp_path: Path):
    status_path = get_agent_status_path(tmp_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "archive_backlog": {
                    "state": "pending",
                    "mode": "drain",
                    "pending_ranges": 3,
                    "ready_ranges": 2,
                    "deferred_ranges": 1,
                    "pending_paths": 2,
                    "pending_sessions": 2,
                    "pending_bytes": 4096,
                    "dead_ranges": 0,
                    "dead_bytes": 0,
                    "huge_pending_ranges": 0,
                    "huge_pending_bytes": 0,
                },
                "adaptive_backlog_limiter": {
                    "current_cap": 2,
                    "ceiling": 16,
                    "pressure_state": "normal",
                    "live_latency_guard_state": "healthy",
                    "last_live_latency_p95_ms": 80,
                    "last_live_enqueue_to_job_p95_ms": 20,
                    "archive_target_batch_bytes": 262144,
                    "ewma_queue_wait_ms": 10.0,
                    "ewma_exec_ms": 20.0,
                    "total_backpressure": 4,
                },
                "ship_scheduler": {
                    "ready_live": 1,
                    "ready_retry": 2,
                    "ready_scan": 3,
                    "in_flight_retry": 1,
                    "in_flight_scan": 0,
                    "backlog_cap": 2,
                    "ready_backlog_bytes": 4096,
                    "in_flight_backlog_bytes": 2048,
                },
                "ship_lanes": {
                    "live": {
                        "attempts_1h": 3,
                        "successes_1h": 3,
                        "connect_errors_1h": 0,
                        "latency_p50_ms_1h": 40,
                        "latency_p95_ms_1h": 80,
                        "last_observed_at_ms": 1_779_000_000_000,
                        "last_http_send_started_at_ms": 1_779_000_000_100,
                        "last_http_finished_at_ms": 1_779_000_000_140,
                        "stage_latency_p95_ms_1h": {
                            "observed_to_http_send_ms": 100,
                            "observed_to_ack_ms": 140,
                            "enqueue_to_job_ms": 20,
                            "http_latency_ms": 40,
                        },
                    },
                    "archive": {
                        "attempts_1h": 8,
                        "successes_1h": 6,
                        "backpressure_1h": 2,
                        "bytes_1h": 1024,
                        "events_1h": 12,
                        "bytes_per_sec_ewma_10s": 512.0,
                        "events_per_sec_ewma_10s": 7.5,
                    },
                },
            }
        )
    )

    summary = collect_archive_backlog(tmp_path)

    assert summary["source"] == "engine_status"
    assert summary["pending_ranges"] == 3
    assert summary["archive_bytes_per_sec"] == 512.0
    assert summary["archive_eta_seconds"] == 8
    assert summary["shipper"]["adaptive_backlog_limiter"]["current_cap"] == 2
    assert summary["shipper"]["ship_scheduler"]["ready_scan"] == 3

    runner = CliRunner()
    result = runner.invoke(app, ["archive", "status", "--state-root", str(tmp_path)])

    assert result.exit_code == 0
    assert "Shipper controller:" in result.stdout
    assert "ready archive 5 (4.0 KB), active archive 1 (2.0 KB)" in result.stdout
    assert "cap 2/16" in result.stdout
    assert "live guard healthy" in result.stdout
    assert "live 1h: 3/3 ok, 0 connect errors, latency p50/p95 40ms/80ms" in result.stdout
    assert "live stages p95: observed->send 100ms, observed->ack 140ms, enqueue->job 20ms, http 40ms" in result.stdout
    assert ("last live: observed 2026-05-17T06:40:00Z, send 2026-05-17T06:40:00.100000Z, ack 2026-05-17T06:40:00.140000Z") in result.stdout

    speed_result = runner.invoke(app, ["archive", "speed", "--state-root", str(tmp_path)])

    assert speed_result.exit_code == 0
    assert "Archive speed" in speed_result.stdout
    assert "archive: 7.5 events/s, 512 B/s, 6/8 ok, 2 backpressure" in speed_result.stdout
    assert "remaining: 4.0 KB, eta 8s at current EWMA" in speed_result.stdout
    assert ("live guardrail: p95 80ms, observed->ack p95 140ms, state healthy, limiter p95 80ms, enqueue->job 20ms") in speed_result.stdout
    assert "scheduler: ready 5 (4.0 KB), active 1 (2.0 KB), cap 2" in speed_result.stdout

    speed_json_result = runner.invoke(app, ["archive", "speed", "--state-root", str(tmp_path), "--json"])

    assert speed_json_result.exit_code == 0
    speed_payload = json.loads(speed_json_result.stdout)
    assert speed_payload["archive"]["pending_bytes"] == 4096
    assert speed_payload["archive"]["bytes_per_sec_ewma_10s"] == 512.0
    assert speed_payload["archive"]["eta_seconds_ewma_10s"] == 8.0
    assert speed_payload["live"]["observed_to_ack_p95_ms_1h"] == 140
    assert speed_payload["live"]["limiter_state"] == "healthy"


def test_archive_status_watch_rejects_json(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["archive", "status", "--watch", "--json", "--state-root", str(tmp_path)])

    assert result.exit_code != 0


def test_archive_pause_is_persistent_but_drain_is_leased(tmp_path: Path):
    paused = write_archive_control(tmp_path, mode="paused")
    assert "expires_at" not in paused

    drain = write_archive_control(tmp_path, mode="drain", lease_minutes=1)
    assert drain["expires_at"] > drain["updated_at"]


def test_archive_control_records_actor_and_reason(tmp_path: Path):
    result = write_archive_control(
        tmp_path,
        mode="drain",
        actor="menu_bar",
        reason="catch up after first install",
    )
    payload = json.loads(Path(result["path"]).read_text())
    assert payload["mode"] == "drain"
    assert payload["actor"] == "menu_bar"
    assert payload["reason"] == "catch up after first install"
    assert payload["expires_at"] > payload["updated_at"]
    assert "max_tick_bytes" not in payload


def test_archive_status_without_engine_status_reports_an_empty_backlog(tmp_path: Path):
    runner = CliRunner()

    result = runner.invoke(app, ["archive", "status", "--state-root", str(tmp_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pending_ranges"] == 0
    assert payload["pending_bytes"] == 0


def test_archive_status_reports_the_storage_v2_outbox_backlog(tmp_path: Path):
    status_path = get_agent_status_path(tmp_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "archive_backlog": {"state": "idle", "mode": "trickle", "pending_bytes": 0},
                "storage_v2_outbox": {
                    "pending_count": 7,
                    "pending_bytes": 8192,
                    "oldest_pending_at": "2026-10-08T00:00:00Z",
                    "blocked_source_count": 1,
                    "reconciling_blocked_source_count": 1,
                    "unresolved_blocked_source_count": 0,
                },
                "ship_lanes": {
                    "archive": {"attempts_1h": 0, "bytes_per_sec_ewma_10s": 0},
                    "repair": {"attempts_1h": 4, "bytes_per_sec_ewma_10s": 1024},
                },
            }
        )
    )

    summary = collect_archive_backlog(tmp_path)

    assert summary["storage_v2_outbox"] == {
        "pending_count": 7,
        "pending_bytes": 8192,
        "oldest_pending_at": "2026-10-08T00:00:00Z",
        "blocked_source_count": 1,
        "reconciling_blocked_source_count": 1,
        "unresolved_blocked_source_count": 0,
    }
    assert summary["archive_eta_seconds"] == 8

    result = CliRunner().invoke(app, ["archive", "status", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "pending envelopes: 7" in result.output
    assert "pending ranges" not in result.output
    assert "1 (0 need you, 1 reconciling)" in result.output

    speed = CliRunner().invoke(app, ["archive", "speed", "--json", "--state-root", str(tmp_path)])
    assert speed.exit_code == 0, speed.output
    speed_archive = json.loads(speed.output)["archive"]
    assert speed_archive["pending_bytes"] == 8192
    assert speed_archive["bytes_per_sec_ewma_10s"] == 1024


def test_archive_status_keeps_an_absent_blocked_split_unknown(tmp_path: Path):
    status_path = get_agent_status_path(tmp_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "archive_backlog": {"state": "idle", "mode": "trickle"},
                "storage_v2_outbox": {"pending_count": 1, "pending_bytes": 10, "blocked_source_count": 2},
                "ship_lanes": {
                    "archive": {"attempts_1h": 3, "bytes_per_sec_ewma_10s": 5},
                    "repair": {"attempts_1h": 1, "bytes_per_sec_ewma_10s": 50},
                },
            }
        )
    )

    summary = collect_archive_backlog(tmp_path)

    assert summary["storage_v2_outbox"]["unresolved_blocked_source_count"] is None
    assert summary["storage_v2_outbox"]["reconciling_blocked_source_count"] is None
    # The busier lane drains the backlog: an older engine still on the archive lane.
    assert summary["archive_bytes_per_sec"] == 5

    result = CliRunner().invoke(app, ["archive", "status", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "blocked sources:  2 (split unknown)" in result.output
