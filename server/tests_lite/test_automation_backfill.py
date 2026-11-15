"""Tests for Hatch automation backfill and catalogd live-host CLI support."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")


def _create_catalogd_sessions_db(db_path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                project TEXT,
                started_at TEXT NOT NULL,
                user_messages INTEGER NOT NULL DEFAULT 0,
                first_user_message_preview TEXT,
                origin_kind TEXT,
                hidden_from_default_timeline INTEGER NOT NULL DEFAULT 0,
                is_subagent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id,
                    provider,
                    project,
                    started_at,
                    user_messages,
                    first_user_message_preview,
                    origin_kind,
                    hidden_from_default_timeline,
                    is_subagent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["session_id"],
                    row["provider"],
                    row.get("project"),
                    row.get("started_at", "2026-11-15T12:00:00Z"),
                    row.get("user_messages", 1),
                    row.get("first_user_message_preview"),
                    row.get("origin_kind"),
                    row.get("hidden_from_default_timeline", 0),
                    row.get("is_subagent", 0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_is_exact_hatch_contract_prompt():
    from zerg.services.agents.automation_backfill import _is_exact_hatch_contract_prompt

    direct = "Hatch execution contract:\nThis is a single bounded, non-interactive run."
    planned = "/plan Hatch execution contract:\nThis is a single bounded, non-interactive run."
    whitespace_planned = "  /plan   \nHatch execution contract:\nThis is a single bounded, non-interactive run."

    assert _is_exact_hatch_contract_prompt(direct) is True
    assert _is_exact_hatch_contract_prompt(planned) is True
    assert _is_exact_hatch_contract_prompt(whitespace_planned) is True
    assert _is_exact_hatch_contract_prompt("Hatch execution contract: wrong second line") is False
    assert _is_exact_hatch_contract_prompt("/plan review the repo") is False
    assert _is_exact_hatch_contract_prompt(None) is False


def test_find_catalogd_hatch_automation_candidates_filters_and_scores(tmp_path: Path):
    from zerg.services.agents.automation_backfill import find_catalogd_hatch_automation_candidates

    db_path = tmp_path / "longhouse-live.db"
    agy_id = str(uuid4())
    pi_id = str(uuid4())
    omp_id = str(uuid4())
    hidden_id = str(uuid4())
    subagent_id = str(uuid4())
    multi_turn_id = str(uuid4())
    human_id = str(uuid4())

    _create_catalogd_sessions_db(
        db_path,
        [
            {
                "session_id": agy_id,
                "provider": "antigravity",
                "project": "zerg",
                "started_at": "2026-11-15T14:00:00Z",
                "user_messages": 1,
                "first_user_message_preview": "/plan Hatch execution contract:\nThis is a single bounded, non-interactive run.",
            },
            {
                "session_id": pi_id,
                "provider": "pi",
                "project": "hatch",
                "started_at": "2026-11-15T13:00:00Z",
                "user_messages": 1,
                "first_user_message_preview": "Hatch execution contract:\nThis is a single bounded, non-interactive run.",
            },
            {
                "session_id": omp_id,
                "provider": "omp",
                "project": "hatch",
                "started_at": "2026-11-15T12:00:00Z",
                "user_messages": 2,
                "first_user_message_preview": "Please do a final review of this branch.",
            },
            {
                "session_id": hidden_id,
                "provider": "antigravity",
                "started_at": "2026-11-15T11:00:00Z",
                "user_messages": 1,
                "hidden_from_default_timeline": 1,
                "first_user_message_preview": "Hatch execution contract:\nThis is a single bounded, non-interactive run.",
            },
            {
                "session_id": subagent_id,
                "provider": "antigravity",
                "started_at": "2026-11-15T10:00:00Z",
                "user_messages": 1,
                "is_subagent": 1,
                "first_user_message_preview": "Hatch execution contract:\nThis is a single bounded, non-interactive run.",
            },
            {
                "session_id": multi_turn_id,
                "provider": "antigravity",
                "started_at": "2026-11-15T09:00:00Z",
                "user_messages": 3,
                "first_user_message_preview": "Hatch execution contract:\nThis is a single bounded, non-interactive run.",
            },
            {
                "session_id": human_id,
                "provider": "antigravity",
                "started_at": "2026-11-15T08:00:00Z",
                "user_messages": 1,
                "first_user_message_preview": "Explain the architecture of Longhouse.",
            },
        ],
    )

    candidates = find_catalogd_hatch_automation_candidates(db_path, limit=50)
    by_id = {c["session_id"]: c for c in candidates}

    assert set(by_id) == {agy_id, pi_id, omp_id}
    assert by_id[agy_id]["confidence"] == "high"
    assert by_id[agy_id]["provider"] == "antigravity"
    assert by_id[pi_id]["confidence"] == "high"
    assert by_id[omp_id]["confidence"] == "medium"


def test_cli_classify_automation_catalogd_without_legacy_engine(tmp_path: Path, monkeypatch):
    import zerg.database as database_mod
    import zerg.services.agents.automation_backfill as backfill_mod
    import zerg.services.catalogd_supervisor as supervisor_mod
    from zerg.cli.main import app

    db_path = tmp_path / "longhouse-live.db"
    agy_id = str(uuid4())
    missing_id = str(uuid4())
    _create_catalogd_sessions_db(
        db_path,
        [
            {
                "session_id": agy_id,
                "provider": "antigravity",
                "project": "zerg",
                "started_at": "2026-11-15T14:30:00Z",
                "user_messages": 1,
                "first_user_message_preview": "/plan Hatch execution contract:\nThis is a single bounded, non-interactive run.",
            }
        ],
    )

    monkeypatch.setattr(database_mod, "default_engine", None)
    monkeypatch.setattr(supervisor_mod, "catalogd_paths", lambda: (db_path, tmp_path / "catalogd.sock"))

    runner = CliRunner()
    dry_run = runner.invoke(
        app,
        ["db", "classify-automation", "--session-id", agy_id, "--session-id", missing_id, "--json"],
    )
    assert dry_run.exit_code == 0, dry_run.output
    dry_payload = json.loads(dry_run.output)
    assert dry_payload["database_url"] == "catalogd"
    assert dry_payload["missing_session_ids"] == [missing_id]
    assert dry_payload["heuristic_candidate_count"] == 1
    assert dry_payload["heuristic_candidates"][0]["session_id"] == agy_id
    assert dry_payload["heuristic_candidates"][0]["confidence"] == "high"

    reclassify_calls: list[tuple[list[str], str]] = []

    def _fake_reclassify(session_ids: list[str], origin_kind: str, *, timeout_seconds: float = 30.0):
        reclassify_calls.append((list(session_ids), origin_kind))
        return {
            "applied_catalogd_session_ids": [sid for sid in session_ids if sid == agy_id],
            "failed_catalogd_session_ids": [],
            "searchd_failed_session_ids": [],
        }

    monkeypatch.setattr(backfill_mod, "reclassify_catalogd_origins", _fake_reclassify)

    apply_run = runner.invoke(
        app,
        ["db", "classify-automation", "--apply", "--session-id", agy_id, "--json"],
    )
    assert apply_run.exit_code == 0, apply_run.output
    assert reclassify_calls == [([agy_id], "hatch_automation")]
    apply_payload = json.loads(apply_run.output)
    assert apply_payload["applied_session_ids"] == [agy_id]
    assert apply_payload["missing_session_ids"] == []
    assert apply_payload["catalogd_applied"] == [agy_id]
    assert apply_payload["catalogd_failed"] == []
    assert apply_payload["searchd_failed"] == []

    # When both legacy engine and catalogd db are missing, command still exits cleanly with code 2.
    monkeypatch.setattr(supervisor_mod, "catalogd_paths", lambda: (tmp_path / "nonexistent.db", tmp_path / "catalogd.sock"))
    no_store_run = runner.invoke(app, ["db", "classify-automation", "--json"])
    assert no_store_run.exit_code == 2
