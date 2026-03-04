"""Tests for explicit heavy SQLite migration planning/runs."""

import os

from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import _migrate_agents_columns
from zerg.database import make_engine
from zerg.db_migrations import apply_heavy_migrations
from zerg.db_migrations import plan_heavy_migrations


def _table_sql(engine, table_name: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'table' AND name = :table_name
                LIMIT 1
                """
            ),
            {"table_name": table_name},
        ).fetchone()
    if row is None or row[0] is None:
        return ""
    return str(row[0])


def _make_legacy_schema(engine) -> None:
    session_id = "00000000-0000-0000-0000-000000000001"
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE sessions (
                id VARCHAR(36) PRIMARY KEY,
                provider VARCHAR(50) NOT NULL,
                environment VARCHAR(20),
                project VARCHAR(255),
                device_id VARCHAR(255),
                cwd TEXT,
                git_repo TEXT,
                git_branch VARCHAR(255),
                started_at DATETIME NOT NULL,
                ended_at DATETIME,
                user_messages INTEGER DEFAULT 0,
                assistant_messages INTEGER DEFAULT 0,
                tool_calls INTEGER DEFAULT 0,
                provider_session_id VARCHAR(255),
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(36) NOT NULL,
                role VARCHAR(20) NOT NULL,
                content_text TEXT,
                tool_name VARCHAR(255),
                tool_input_json JSON,
                tool_output_text TEXT,
                timestamp DATETIME NOT NULL,
                source_path TEXT,
                source_offset INTEGER,
                event_hash VARCHAR(255),
                schema_version VARCHAR(20),
                raw_json TEXT
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE source_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(36) NOT NULL,
                source_path TEXT NOT NULL,
                source_offset BIGINT NOT NULL,
                raw_json TEXT NOT NULL,
                line_hash VARCHAR(64) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, source_path, source_offset)
            )
            """
        )
        conn.execute(
            text(
                """
                INSERT INTO sessions (
                    id, provider, environment, started_at, user_messages, assistant_messages, tool_calls
                ) VALUES (
                    :id, 'claude', 'test', CURRENT_TIMESTAMP, 0, 0, 0
                )
                """
            ),
            {"id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO events (
                    session_id, role, content_text, timestamp, source_path, source_offset, event_hash, raw_json
                ) VALUES (
                    :id, 'user', 'hello', CURRENT_TIMESTAMP, '/tmp/s.jsonl', 1, 'abc', '{"type":"user"}'
                )
                """
            ),
            {"id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO source_lines (
                    session_id, source_path, source_offset, raw_json, line_hash
                ) VALUES (
                    :id, '/tmp/s.jsonl', 1, '{"type":"user"}', 'linehash1'
                )
                """
            ),
            {"id": session_id},
        )


def test_heavy_migration_plan_detects_legacy_pending(tmp_path):
    db_path = tmp_path / "legacy_pending.db"
    engine = make_engine(f"sqlite:///{db_path}")
    _make_legacy_schema(engine)

    # Startup-safe migration should add lightweight columns only.
    _migrate_agents_columns(engine)

    plan = plan_heavy_migrations(engine)
    pending_names = {item.name for item in plan if item.pending}
    assert "20260304_events_branch_backfill" in pending_names
    assert "20260304_source_lines_branch_revision_rebuild" in pending_names


def test_apply_heavy_migrations_is_idempotent_and_records_ledger(tmp_path):
    db_path = tmp_path / "legacy_apply.db"
    engine = make_engine(f"sqlite:///{db_path}")
    _make_legacy_schema(engine)
    _migrate_agents_columns(engine)

    first_run = apply_heavy_migrations(engine)
    assert any(item.name == "20260304_events_branch_backfill" and item.status == "applied" for item in first_run)
    assert any(item.name == "20260304_source_lines_branch_revision_rebuild" and item.status == "applied" for item in first_run)

    pending_after_first = [item.name for item in plan_heavy_migrations(engine) if item.pending]
    assert pending_after_first == []

    second_run = apply_heavy_migrations(engine)
    assert all(item.status == "skipped" for item in second_run)

    with engine.connect() as conn:
        null_branch_rows = int(conn.execute(text("SELECT COUNT(*) FROM events WHERE branch_id IS NULL")).scalar() or 0)
        ledger_rows = conn.execute(
            text("SELECT migration_name, status FROM migration_runs ORDER BY migration_name")
        ).fetchall()
    assert null_branch_rows == 0
    assert ledger_rows == [
        ("20260304_events_branch_backfill", "succeeded"),
        ("20260304_source_lines_branch_revision_rebuild", "succeeded"),
    ]

    normalized_sql = "".join(ch for ch in _table_sql(engine, "source_lines").lower() if not ch.isspace())
    assert "unique(session_id,source_path,source_offset)" not in normalized_sql
