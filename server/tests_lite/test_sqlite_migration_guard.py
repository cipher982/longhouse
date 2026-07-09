"""Guardrails for SQLite column migrations on existing agents tables.

This test simulates a pre-migration database where tables already exist but
lack newer columns. `_migrate_agents_columns()` must backfill all model columns
that existing deployments expect.
"""

import os

from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import _auto_add_missing_columns
from zerg.database import _migrate_agents_columns as _migrate_agents_columns_raw
from zerg.database import _pre_migrate_session_inputs_identity_columns
from zerg.database import make_engine


def _migrate_agents_columns(engine):
    """Mirror the production startup migration sequence (Phase 2).

    ``initialize_database`` runs ``Base.metadata.create_all`` first (creating
    any tables that don't yet exist with their full modern schema), then
    ``_auto_add_missing_columns`` (drift on existing tables), then the
    residual imperative blocks. Tests that exercise the legacy upgrade path
    must do the same.
    """

    Base.metadata.create_all(bind=engine)
    _pre_migrate_session_inputs_identity_columns(engine)
    _auto_add_missing_columns(engine, Base.metadata, apply=True)
    _migrate_agents_columns_raw(engine)


from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionTurn
from zerg.models.agents import TimelineCard


def _table_columns(engine, table_name: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {row[1] for row in rows}


def test_sqlite_migration_adds_role_preview_columns(tmp_path):
    db_path = tmp_path / "role_preview_column_migration.db"
    engine = make_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE sessions (
                id VARCHAR(36) PRIMARY KEY,
                provider VARCHAR(50) NOT NULL,
                environment VARCHAR(20) NOT NULL,
                started_at DATETIME NOT NULL,
                first_user_message_preview TEXT,
                last_visible_text_preview TEXT
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE timeline_cards (
                session_id VARCHAR(36) PRIMARY KEY,
                provider VARCHAR(50) NOT NULL,
                environment VARCHAR(20) NOT NULL,
                started_at DATETIME NOT NULL,
                first_user_message_preview TEXT,
                last_visible_text_preview TEXT,
                parser_revision VARCHAR(128) NOT NULL
            )
            """
        )

    _migrate_agents_columns(engine)

    session_columns = _table_columns(engine, "sessions")
    timeline_card_columns = _table_columns(engine, "timeline_cards")
    assert {col.name for col in AgentSession.__table__.columns if col.name.endswith("_message_preview")} <= session_columns
    assert {col.name for col in TimelineCard.__table__.columns if col.name.endswith("_message_preview")} <= timeline_card_columns
    assert {"launch_actor", "launch_surface"} <= session_columns
    assert {"launch_actor", "launch_surface"} <= timeline_card_columns


def test_sqlite_migration_renames_session_input_request_identity(tmp_path):
    db_path = tmp_path / "session_input_identity_migration.db"
    engine = make_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE session_inputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(36) NOT NULL,
                text TEXT NOT NULL,
                owner_id INTEGER,
                intent VARCHAR(16) NOT NULL,
                status VARCHAR(16) NOT NULL,
                request_id VARCHAR(64),
                last_error TEXT,
                created_at DATETIME,
                delivered_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE UNIQUE INDEX ix_session_inputs_session_owner_request
            ON session_inputs(session_id, owner_id, request_id)
            WHERE request_id IS NOT NULL
            """
        )
        conn.execute(
            text(
                """
                INSERT INTO session_inputs (
                    session_id,
                    text,
                    owner_id,
                    intent,
                    status,
                    request_id
                )
                VALUES (
                    'session-1',
                    'hello',
                    7,
                    'queue',
                    'queued',
                    'ios-existing-1'
                )
                """
            )
        )

    _migrate_agents_columns(engine)

    columns = _table_columns(engine, "session_inputs")
    expected_columns = {col.name for col in SessionInput.__table__.columns}
    assert expected_columns <= columns
    assert "request_id" not in columns

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT client_request_id, delivery_request_id
                FROM session_inputs
                WHERE id = 1
                """
            )
        ).one()
        indexes = conn.execute(text("PRAGMA index_list(session_inputs)")).fetchall()

    assert row.client_request_id == "ios-existing-1"
    assert row.delivery_request_id is None
    assert any(index[1] == "ix_session_inputs_session_owner_client_request" for index in indexes)
    assert all(index[1] != "ix_session_inputs_session_owner_request" for index in indexes)


def test_sqlite_migration_adds_provider_session_alias_unique_index(tmp_path):
    db_path = tmp_path / "provider_session_alias_index_migration.db"
    engine = make_engine(f"sqlite:///{db_path}")

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
                started_at DATETIME NOT NULL,
                user_messages INTEGER DEFAULT 0,
                assistant_messages INTEGER DEFAULT 0,
                tool_calls INTEGER DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE session_threads (
                id VARCHAR(36) PRIMARY KEY,
                session_id VARCHAR(36) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                parent_thread_id VARCHAR(36),
                branch_kind VARCHAR(32) NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE session_thread_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id VARCHAR(36) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                alias_kind VARCHAR(48) NOT NULL,
                alias_value VARCHAR(1024) NOT NULL,
                first_seen_at DATETIME,
                last_seen_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE session_runs (
                id VARCHAR(36) PRIMARY KEY,
                thread_id VARCHAR(36) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                host_id VARCHAR(255),
                started_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE session_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(36) NOT NULL,
                control_plane VARCHAR(64) NOT NULL,
                acquisition_kind VARCHAR(32) NOT NULL,
                state VARCHAR(32) NOT NULL,
                can_send_input INTEGER
            )
            """
        )
        conn.execute(
            text(
                """
                INSERT INTO sessions (id, provider, environment, started_at)
                VALUES
                  ('session-old', 'opencode', 'development', '2026-06-23T12:00:00Z'),
                  ('session-managed', 'opencode', 'development', '2026-06-23T12:00:00Z')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO session_threads (id, session_id, provider, branch_kind, is_primary, created_at)
                VALUES
                  ('thread-old', 'session-old', 'opencode', 'root', 1, '2026-06-23T12:00:00Z'),
                  ('thread-managed', 'session-managed', 'opencode', 'root', 1, '2026-06-23T12:00:01Z')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO session_thread_aliases (thread_id, provider, alias_kind, alias_value)
                VALUES
                  ('thread-old', 'opencode', 'provider_session_id', 'ses-shared'),
                  ('thread-managed', 'opencode', 'provider_session_id', 'ses-shared'),
                  ('thread-missing', 'opencode', 'provider_session_id', 'ses-shared')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO session_runs (id, thread_id, provider)
                VALUES ('run-managed', 'thread-managed', 'opencode')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO session_connections (run_id, control_plane, acquisition_kind, state, can_send_input)
                VALUES ('run-managed', 'opencode_bridge', 'spawned_control', 'attached', 1)
                """
            )
        )

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        aliases = conn.execute(
            text(
                """
                SELECT thread_id
                FROM session_thread_aliases
                WHERE provider = 'opencode'
                  AND alias_kind = 'provider_session_id'
                  AND alias_value = 'ses-shared'
                """
            )
        ).fetchall()
        indexes = conn.execute(text("PRAGMA index_list(session_thread_aliases)")).fetchall()

    assert [row.thread_id for row in aliases] == ["thread-managed"]
    assert any(index[1] == "ux_thread_aliases_provider_session_routing" for index in indexes)


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
        conn.exec_driver_sql(
            """
            CREATE TABLE session_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(36) NOT NULL,
                request_id VARCHAR(64),
                state VARCHAR(20) NOT NULL,
                terminal_phase VARCHAR(32),
                error_code VARCHAR(64),
                user_event_id INTEGER,
                durable_assistant_event_id INTEGER,
                baseline_event_id INTEGER,
                baseline_runtime_cursor INTEGER,
                user_submitted_at DATETIME NOT NULL,
                send_accepted_at DATETIME,
                active_phase_observed_at DATETIME,
                terminal_at DATETIME,
                durable_at DATETIME,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO session_turns (
                session_id,
                request_id,
                state,
                baseline_runtime_cursor,
                user_submitted_at
            )
            VALUES ('session-1', 'req-1', 'created', 42, '2026-05-12T16:00:00Z')
            """
        )

    _migrate_agents_columns(engine)

    session_columns = _table_columns(engine, "sessions")
    event_columns = _table_columns(engine, "events")
    branch_columns = _table_columns(engine, "session_branches")
    source_line_columns = _table_columns(engine, "source_lines")
    observation_columns = _table_columns(engine, "session_observations")
    session_turn_columns = _table_columns(engine, "session_turns")

    expected_session_columns = {col.name for col in AgentSession.__table__.columns}
    expected_event_columns = {col.name for col in AgentEvent.__table__.columns}
    expected_branch_columns = {col.name for col in AgentSessionBranch.__table__.columns}
    expected_source_line_columns = {col.name for col in AgentSourceLine.__table__.columns}
    expected_observation_columns = {col.name for col in SessionObservation.__table__.columns}
    expected_session_turn_columns = {col.name for col in SessionTurn.__table__.columns}

    missing_session_columns = sorted(expected_session_columns - session_columns)
    missing_event_columns = sorted(expected_event_columns - event_columns)
    missing_branch_columns = sorted(expected_branch_columns - branch_columns)
    missing_source_line_columns = sorted(expected_source_line_columns - source_line_columns)
    missing_observation_columns = sorted(expected_observation_columns - observation_columns)
    missing_session_turn_columns = sorted(expected_session_turn_columns - session_turn_columns)

    assert not missing_session_columns, f"sessions migration missing columns: {missing_session_columns}"
    assert not missing_event_columns, f"events migration missing columns: {missing_event_columns}"
    assert not missing_branch_columns, f"session_branches migration missing columns: {missing_branch_columns}"
    assert not missing_source_line_columns, f"source_lines migration missing columns: {missing_source_line_columns}"
    assert not missing_observation_columns, (
        f"session_observations migration missing columns: {missing_observation_columns}"
    )
    assert not missing_session_turn_columns, f"session_turns migration missing columns: {missing_session_turn_columns}"

    with engine.connect() as conn:
        copied_cursor = conn.execute(
            text("SELECT baseline_observation_cursor FROM session_turns WHERE request_id='req-1'")
        ).scalar_one()
    assert copied_cursor == 42


def test_sqlite_migration_backfills_launch_attempt_owner_id(tmp_path):
    db_path = tmp_path / "launch_attempt_owner_migration.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_id = "11111111-1111-4111-8111-111111111111"

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE sessions (
                id VARCHAR(36) PRIMARY KEY,
                owner_id INTEGER,
                provider VARCHAR(50) NOT NULL,
                started_at DATETIME NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE session_launch_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(36) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                device_id VARCHAR(255),
                client_request_id VARCHAR(64),
                state VARCHAR(32) NOT NULL DEFAULT 'pending',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            text(
                """
                INSERT INTO sessions (id, owner_id, provider, started_at)
                VALUES (:session_id, 42, 'codex', '2026-05-23T12:00:00Z')
                """
            ),
            {"session_id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO session_launch_attempts (session_id, provider, device_id, client_request_id)
                VALUES (:session_id, 'codex', 'devbox', 'tap-1')
                """
            ),
            {"session_id": session_id},
        )

    _migrate_agents_columns(engine)

    columns = _table_columns(engine, "session_launch_attempts")
    assert "owner_id" in columns

    with engine.connect() as conn:
        row = conn.execute(text("SELECT owner_id FROM session_launch_attempts WHERE client_request_id = 'tap-1'")).one()
        indexes = conn.execute(text("PRAGMA index_list(session_launch_attempts)")).fetchall()

    assert row.owner_id == 42
    assert any(index[1] == "ix_session_launch_attempts_owner_request" for index in indexes)
