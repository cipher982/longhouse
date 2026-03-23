"""Guardrails for SQLite column migrations on existing agents tables.

This test simulates a pre-migration database where tables already exist but
lack newer columns. `_migrate_agents_columns()` must backfill all model columns
that existing deployments expect.
"""

import os

from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import _migrate_agents_columns
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import AgentSession
from zerg.models.models import JobRun


def _table_columns(engine, table_name: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {row[1] for row in rows}


def test_sqlite_migration_adds_current_model_columns(tmp_path):
    db_path = tmp_path / "migration_guard.db"
    engine = make_engine(f"sqlite:///{db_path}")

    # Simulate older tables that predate recent ALTER TABLE migrations.
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
            CREATE TABLE job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL,
                started_at DATETIME,
                finished_at DATETIME,
                duration_ms INTEGER,
                error_message TEXT,
                metadata_json JSON,
                created_at DATETIME
            )
            """
        )

    _migrate_agents_columns(engine)

    session_columns = _table_columns(engine, "sessions")
    event_columns = _table_columns(engine, "events")
    branch_columns = _table_columns(engine, "session_branches")
    source_line_columns = _table_columns(engine, "source_lines")
    job_run_columns = _table_columns(engine, "job_runs")

    expected_session_columns = {col.name for col in AgentSession.__table__.columns}
    expected_event_columns = {col.name for col in AgentEvent.__table__.columns}
    expected_branch_columns = {col.name for col in AgentSessionBranch.__table__.columns}
    expected_source_line_columns = {col.name for col in AgentSourceLine.__table__.columns}
    expected_job_run_columns = {col.name for col in JobRun.__table__.columns}

    missing_session_columns = sorted(expected_session_columns - session_columns)
    missing_event_columns = sorted(expected_event_columns - event_columns)
    missing_branch_columns = sorted(expected_branch_columns - branch_columns)
    missing_source_line_columns = sorted(expected_source_line_columns - source_line_columns)
    missing_job_run_columns = sorted(expected_job_run_columns - job_run_columns)

    assert not missing_session_columns, f"sessions migration missing columns: {missing_session_columns}"
    assert not missing_event_columns, f"events migration missing columns: {missing_event_columns}"
    assert not missing_branch_columns, f"session_branches migration missing columns: {missing_branch_columns}"
    assert not missing_source_line_columns, f"source_lines migration missing columns: {missing_source_line_columns}"
    assert not missing_job_run_columns, f"job_runs migration missing columns: {missing_job_run_columns}"
