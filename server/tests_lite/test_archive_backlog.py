from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from zerg.cli.main import app
from zerg.services.archive_backlog import collect_archive_backlog
from zerg.services.archive_backlog import inspect_archive_backlog
from zerg.services.archive_backlog import write_archive_control
from zerg.services.longhouse_paths import get_agent_db_path


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
