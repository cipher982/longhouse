from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from zerg.cli.main import app
from zerg.services.archive_backlog import collect_archive_backlog
from zerg.services.archive_backlog import inspect_archive_backlog
from zerg.services.archive_backlog import ready_archive_backlog
from zerg.services.archive_backlog import write_archive_control
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_state_dir
from zerg.services.longhouse_paths import get_agent_status_path


def _create_spool_db(state_root: Path) -> None:
    db_path = get_agent_db_path(state_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE spool_queue (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                next_retry_at TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO spool_queue
              (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, status)
            VALUES
              ('codex', '/tmp/a.jsonl', 0, 1048576, 's1', '2026-06-01T00:00:00Z', '2026-06-02T00:00:00Z', 'pending'),
              ('codex', '/tmp/a.jsonl', 1048576, 2097152, 's1',
               '2026-06-01T00:01:00Z', '2026-06-02T00:01:00Z', 'pending'),
              ('claude', '/tmp/dead.jsonl', 0, 10, 's2', '2026-06-01T00:02:00Z', '2026-06-02T00:02:00Z', 'dead')
            """
        )
        conn.commit()


def test_collect_archive_backlog_summarizes_sqlite_spool(tmp_path: Path):
    _create_spool_db(tmp_path)

    summary = collect_archive_backlog(tmp_path)

    assert summary["state"] == "dead_lettered"
    assert summary["pending_ranges"] == 2
    assert summary["pending_paths"] == 1
    assert summary["pending_sessions"] == 1
    assert summary["pending_bytes"] == 2 * 1024 * 1024
    assert summary["dead_ranges"] == 1
    assert summary["mode"] == "drain"
    assert summary["providers"][0]["provider"] == "codex"


def test_archive_inspect_and_control(tmp_path: Path):
    _create_spool_db(tmp_path)

    rows = inspect_archive_backlog(tmp_path, limit=1)
    assert rows == [
        {
            "provider": "codex",
            "file_path": "/tmp/a.jsonl",
            "pending_ranges": 2,
            "pending_sessions": 1,
            "pending_bytes": 2 * 1024 * 1024,
            "oldest_pending_at": "2026-06-01T00:00:00Z",
            "newest_pending_at": "2026-06-01T00:01:00Z",
            "next_retry_at_min": "2026-06-02T00:00:00Z",
            "last_error": None,
        }
    ]

    result = write_archive_control(tmp_path, mode="drain", max_tick_bytes=123, include_huge=True)
    payload = json.loads(Path(result["path"]).read_text())
    assert payload["mode"] == "drain"
    assert payload["max_tick_bytes"] == 123
    assert payload["include_huge"] is True


def test_archive_status_cli_reads_state_root(tmp_path: Path):
    _create_spool_db(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["archive", "status", "--state-root", str(tmp_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "drain"
    assert payload["pending_ranges"] == 2
    assert payload["pending_bytes"] == 2 * 1024 * 1024


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
    assert (
        "last live: observed 2026-05-17T06:40:00Z, send 2026-05-17T06:40:00.100000Z, ack 2026-05-17T06:40:00.140000Z"
    ) in result.stdout

    speed_result = runner.invoke(app, ["archive", "speed", "--state-root", str(tmp_path)])

    assert speed_result.exit_code == 0
    assert "Archive speed" in speed_result.stdout
    assert "archive: 7.5 events/s, 512 B/s, 6/8 ok, 2 backpressure" in speed_result.stdout
    assert (
        "live guardrail: p95 80ms, observed->ack p95 140ms, state healthy, limiter p95 80ms, enqueue->job 20ms"
    ) in speed_result.stdout
    assert "scheduler: ready 5 (4.0 KB), active 1 (2.0 KB), cap 2" in speed_result.stdout

    speed_json_result = runner.invoke(app, ["archive", "speed", "--state-root", str(tmp_path), "--json"])

    assert speed_json_result.exit_code == 0
    speed_payload = json.loads(speed_json_result.stdout)
    assert speed_payload["archive"]["bytes_per_sec_ewma_10s"] == 512.0
    assert speed_payload["live"]["observed_to_ack_p95_ms_1h"] == 140
    assert speed_payload["live"]["limiter_state"] == "healthy"


def test_archive_status_watch_rejects_json(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["archive", "status", "--watch", "--json", "--state-root", str(tmp_path)])

    assert result.exit_code != 0


def test_archive_pause_class_huge_keeps_non_huge_drain_enabled(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["archive", "pause", "--class", "huge", "--state-root", str(tmp_path)])

    assert result.exit_code == 0
    assert "huge-range replay paused" in result.stdout
    payload = json.loads((get_agent_state_dir(tmp_path) / "archive-repair-control.json").read_text())
    assert payload["mode"] == "drain"
    assert payload["include_huge"] is False


def test_ready_archive_backlog_makes_pending_ranges_eligible(tmp_path: Path):
    _create_spool_db(tmp_path)

    changed = ready_archive_backlog(tmp_path)

    assert changed == 2
    db_path = get_agent_db_path(tmp_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, next_retry_at FROM spool_queue ORDER BY id",
        ).fetchall()

    assert rows[0][0] == "pending"
    assert rows[1][0] == "pending"
    assert rows[0][1] == rows[1][1]
    assert rows[2][0] == "dead"


def test_archive_drain_retry_now_cli_resets_pending_clocks(tmp_path: Path):
    _create_spool_db(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["archive", "drain", "--state-root", str(tmp_path), "--retry-now"])

    assert result.exit_code == 0
    assert "Archive retry clocks reset for 2 pending range(s)." in result.stdout


def test_archive_drain_max_safe_excludes_huge_ranges(tmp_path: Path):
    runner = CliRunner()

    result = runner.invoke(app, ["archive", "drain", "--target", "max-safe", "--state-root", str(tmp_path)])

    assert result.exit_code == 0
    assert "Archive repair max-safe drain enabled" in result.stdout
    payload = json.loads((get_agent_state_dir(tmp_path) / "archive-repair-control.json").read_text())
    assert payload["mode"] == "drain"
    assert payload["max_tick_bytes"] == 4 * 1024 * 1024 * 1024
    assert payload["include_huge"] is False


def test_archive_inspect_largest_cli_is_explicit(tmp_path: Path):
    _create_spool_db(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["archive", "inspect", "--largest", "--limit", "1", "--state-root", str(tmp_path)])

    assert result.exit_code == 0
    assert "codex 2.0 MB 2 range(s) /tmp/a.jsonl" in result.stdout
