from __future__ import annotations

import json
import sqlite3

from typer.testing import CliRunner

from zerg.cli.shipping import app
from zerg.cli.shipping import _source_rows


def test_shipping_inspect_reads_exact_retained_source_proof(tmp_path):
    database = tmp_path / "agent" / "longhouse-shipper.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE source_epoch_registry (
                source_epoch TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                opaque_source_id TEXT NOT NULL
            );
            CREATE TABLE pending_source_envelope (
                source_epoch TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                range_start INTEGER NOT NULL,
                range_end INTEGER NOT NULL,
                envelope_id TEXT NOT NULL,
                raw_bytes INTEGER NOT NULL,
                event_count INTEGER NOT NULL,
                has_reply_evidence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                last_attempt_at TEXT,
                blocked_at TEXT,
                block_kind TEXT,
                block_detail TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO source_epoch_registry VALUES (?, ?, ?)",
            ("epoch-1", "claude", "session-1"),
        )
        connection.execute(
            """
            INSERT INTO pending_source_envelope VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "epoch-1",
                "/tmp/session.jsonl",
                10,
                20,
                "envelope-1",
                100,
                2,
                1,
                "2026-08-03T20:00:00Z",
                3,
                "2026-08-03T20:01:00Z",
                "2026-08-03T20:01:00Z",
                "source_epoch_conflict_unresolved",
                "source proof is retained",
            ),
        )

    resolved_database, rows = _source_rows(tmp_path, "epoch-1", 10)

    assert resolved_database == database
    assert rows[0]["provider"] == "claude"
    assert rows[0]["opaque_source_id"] == "session-1"
    assert rows[0]["block_kind"] == "source_epoch_conflict_unresolved"


def test_shipping_inspect_reports_missing_database_in_machine_output(tmp_path):
    result = CliRunner().invoke(app, ["--state-root", str(tmp_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["database_exists"] is False
    assert payload["rows"] == []
