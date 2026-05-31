"""Tests for explicit heavy SQLite migration planning/runs."""

import os

from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import _auto_add_missing_columns
from zerg.database import _migrate_agents_columns as _migrate_agents_columns_raw
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.db_migrations import apply_heavy_migrations
from zerg.db_migrations import plan_heavy_migrations


def _migrate_agents_columns(engine):
    """Run the Phase 2 startup migration sequence used by ``initialize_database``.

    Phase 2 split additive ALTERs into ``_auto_add_missing_columns`` (which runs
    first against ``Base.metadata``) and kept non-additive blocks in
    ``_migrate_agents_columns_raw``. Tests that previously called the imperative
    function directly need to mirror the production ordering.
    """

    _auto_add_missing_columns(engine, Base.metadata, apply=True)
    _migrate_agents_columns_raw(engine)


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


def test_startup_migration_adds_insight_origin_and_backfills_system_rows(tmp_path):
    db_path = tmp_path / "legacy_insights.db"
    engine = make_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE insights (
                id VARCHAR(36) PRIMARY KEY,
                insight_type VARCHAR(20) NOT NULL,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                project VARCHAR(255),
                severity VARCHAR(20),
                confidence FLOAT,
                tags TEXT,
                observations TEXT,
                session_id VARCHAR(36),
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.execute(
            text(
                """
                INSERT INTO insights (id, insight_type, title, description, tags, created_at, updated_at)
                VALUES
                    ('1', 'learning', 'Manual note', 'keep visible', '["docs"]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    (
                        '2', 'failure', 'Agent stale', 'system row by tag',
                        '["engine","stale-agent"]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    ),
                    (
                        '3', 'failure', 'Stale ingest detected', 'system row by title',
                        NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    ),
                    (
                        '4', 'learning', 'Ingest recovered', 'system row by title',
                        NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                """
            )
        )

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(insights)"))}
        rows = conn.execute(text("SELECT title, origin FROM insights ORDER BY id")).fetchall()

    assert "origin" in columns
    assert rows == [
        ("Manual note", None),
        ("Agent stale", "system"),
        ("Stale ingest detected", "system"),
        ("Ingest recovered", "system"),
    ]


def test_startup_migration_adds_runner_availability_policy_and_backfills_defaults(tmp_path):
    db_path = tmp_path / "legacy_runners.db"
    engine = make_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE runners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                name VARCHAR NOT NULL,
                labels JSON,
                capabilities JSON NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'offline',
                last_seen_at DATETIME,
                auth_secret_hash VARCHAR NOT NULL,
                runner_metadata JSON,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO runners (owner_id, name, capabilities, status, auth_secret_hash, runner_metadata)
            VALUES
              (1, 'cinder', '["exec.full"]', 'offline', 'hash1', '{"install_mode":"desktop"}'),
              (1, 'demo-runner', '["exec.full"]', 'offline', 'hash2', '{"install_mode":"server"}'),
              (1, 'lh-vm-canary-20260317', '["exec.full"]', 'offline', 'hash3', '{"install_mode":"server"}')
            """
        )

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(runners)"))}
        rows = conn.execute(text("SELECT name, availability_policy FROM runners ORDER BY id")).fetchall()

    assert "availability_policy" in columns
    assert rows == [
        ("cinder", "on_demand"),
        ("demo-runner", "always_on"),
        ("lh-vm-canary-20260317", "ephemeral"),
    ]


def test_startup_migration_adds_session_execution_home_columns(tmp_path):
    import pytest

    pytest.skip(
        "session-identity-kernel cleanup: execution_home/managed_transport/source_runner_* "
        "columns were removed; transport now derives from session_connections.control_plane."
    )
    db_path = tmp_path / "legacy_sessions.db"
    engine = make_engine(f"sqlite:///{db_path}")

    _make_legacy_schema(engine)

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))}
        row = conn.execute(
            text(
                """
                SELECT execution_home, managed_transport, source_runner_id, source_runner_name,
                       managed_session_name
                FROM sessions
                LIMIT 1
                """
            )
        ).fetchone()

    assert "execution_home" in columns
    assert "managed_transport" in columns
    assert "source_runner_id" in columns
    assert "source_runner_name" in columns
    assert "managed_session_name" in columns
    assert "managed_tmux_tmpdir" not in columns
    assert "managed_launch_profile" not in columns
    assert row == ("unmanaged_local", None, None, None, None)


def test_startup_migration_adds_session_loop_mode_and_backfills_assist(tmp_path):
    import pytest

    pytest.skip(
        "session-identity-kernel cleanup: loop_mode/loop_thread_id columns were removed; "
        "loop continuations are no longer modeled on AgentSession."
    )
    db_path = tmp_path / "legacy_sessions_loop_mode.db"
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
            INSERT INTO sessions (id, provider, environment, started_at, user_messages, assistant_messages, tool_calls)
            VALUES ('00000000-0000-0000-0000-000000000123', 'claude', 'production', CURRENT_TIMESTAMP, 1, 1, 0)
            """
        )

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(sessions)"))}
        rows = conn.execute(text("SELECT id, loop_mode, loop_thread_id FROM sessions")).fetchall()

    assert "loop_mode" in columns
    assert "loop_thread_id" in columns
    assert rows == [("00000000-0000-0000-0000-000000000123", "assist", None)]


def test_startup_migration_clears_progress_only_runtime_live_timestamps(tmp_path):
    db_path = tmp_path / "runtime_state_truth.db"
    engine = make_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE session_runtime_state (
                runtime_key VARCHAR(255) PRIMARY KEY,
                session_id CHAR(36),
                provider VARCHAR(64) NOT NULL,
                device_id VARCHAR(255),
                phase VARCHAR(32) NOT NULL,
                phase_source VARCHAR(32) NOT NULL,
                active_tool VARCHAR(128),
                phase_started_at DATETIME,
                last_runtime_signal_at DATETIME,
                last_progress_at DATETIME,
                last_live_at DATETIME,
                timeline_anchor_at DATETIME NOT NULL,
                freshness_expires_at DATETIME,
                terminal_state VARCHAR(32),
                terminal_at DATETIME,
                runtime_version INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO session_runtime_state (
                runtime_key, provider, phase, phase_source, active_tool,
                last_runtime_signal_at, last_progress_at, last_live_at,
                timeline_anchor_at, freshness_expires_at, terminal_state
            ) VALUES
              (
                'opencode:progress-only', 'opencode', 'running', 'progress', 'bash',
                '2026-05-04 17:40:44', '2026-05-04 17:40:44', '2026-05-04 17:40:44',
                '2026-05-04 17:40:44', '2026-05-04 17:41:44', NULL
              ),
              (
                'codex:phase-truth', 'codex', 'running', 'semantic', 'edit',
                '2026-05-04 18:00:00', '2026-05-04 18:00:00', '2026-05-04 18:00:00',
                '2026-05-04 18:00:00', '2026-05-04 18:01:00', NULL
              )
            """
        )

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT runtime_key, phase, active_tool, last_runtime_signal_at,
                       last_progress_at, last_live_at, freshness_expires_at,
                       terminal_reason, terminal_source
                FROM session_runtime_state
                ORDER BY runtime_key
                """
            )
        ).fetchall()

    assert rows == [
        (
            "codex:phase-truth",
            "running",
            "edit",
            "2026-05-04 18:00:00",
            "2026-05-04 18:00:00",
            "2026-05-04 18:00:00",
            "2026-05-04 18:01:00",
            None,
            None,
        ),
        ("opencode:progress-only", "idle", None, None, "2026-05-04 17:40:44", None, None, None, None),
    ]


def test_startup_migration_only_backfills_irreversible_terminal_states(tmp_path):
    db_path = tmp_path / "terminal_backfill.db"
    engine = make_engine(f"sqlite:///{db_path}")

    host_expired_id = "00000000-0000-0000-0000-000000000201"
    process_gone_id = "00000000-0000-0000-0000-000000000202"
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE sessions (
                id VARCHAR(36) PRIMARY KEY,
                provider VARCHAR(50) NOT NULL,
                environment VARCHAR(20),
                started_at DATETIME NOT NULL,
                ended_at DATETIME,
                execution_home VARCHAR(32),
                source_runner_id INTEGER,
                thread_root_session_id VARCHAR(36),
                is_writable_head BOOLEAN DEFAULT 1,
                continued_from_session_id VARCHAR(36),
                loop_mode VARCHAR(32),
                user_messages INTEGER DEFAULT 0,
                assistant_messages INTEGER DEFAULT 0,
                tool_calls INTEGER DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE session_runtime_state (
                runtime_key VARCHAR(255) PRIMARY KEY,
                session_id CHAR(36),
                provider VARCHAR(64) NOT NULL,
                device_id VARCHAR(255),
                phase VARCHAR(32) NOT NULL,
                phase_source VARCHAR(32) NOT NULL,
                active_tool VARCHAR(128),
                phase_started_at DATETIME,
                last_runtime_signal_at DATETIME,
                last_progress_at DATETIME,
                last_live_at DATETIME,
                timeline_anchor_at DATETIME NOT NULL,
                freshness_expires_at DATETIME,
                terminal_state VARCHAR(32),
                terminal_at DATETIME,
                runtime_version INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            text(
                """
                INSERT INTO sessions (
                    id, provider, environment, started_at, user_messages, assistant_messages, tool_calls
                ) VALUES
                  (:host_expired_id, 'claude', 'test', '2026-05-04 17:00:00', 1, 1, 0),
                  (:process_gone_id, 'claude', 'test', '2026-05-04 17:00:00', 1, 1, 0)
                """
            ),
            {"host_expired_id": host_expired_id, "process_gone_id": process_gone_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO session_runtime_state (
                    runtime_key, session_id, provider, phase, phase_source,
                    last_runtime_signal_at, last_live_at, timeline_anchor_at,
                    terminal_state, terminal_at
                ) VALUES
                  (
                    'claude:host-expired', :host_expired_id, 'claude', 'finished', 'semantic',
                    '2026-05-04 17:30:00', '2026-05-04 17:30:00', '2026-05-04 17:30:00',
                    'host_expired', '2026-05-04 17:30:00'
                  ),
                  (
                    'claude:process-gone', :process_gone_id, 'claude', 'finished', 'semantic',
                    '2026-05-04 17:40:00', '2026-05-04 17:40:00', '2026-05-04 17:40:00',
                    'process_gone', '2026-05-04 17:40:00'
                  )
                """
            ),
            {"host_expired_id": host_expired_id, "process_gone_id": process_gone_id},
        )

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, ended_at FROM sessions ORDER BY id")).fetchall()

    assert rows == [
        (host_expired_id, None),
        (process_gone_id, "2026-05-04 17:40:00"),
    ]


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


def test_session_identity_kernel_backfill_is_explicit_heavy_migration(tmp_path):
    db_path = tmp_path / "identity_kernel.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)

    session_id = "00000000-0000-0000-0000-000000000111"
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO sessions (
                    id, provider, environment, started_at, user_messages, assistant_messages, tool_calls
                ) VALUES (
                    :session_id, 'claude', 'test', CURRENT_TIMESTAMP, 1, 0, 0
                )
                """
            ),
            {"session_id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO events (
                    session_id, role, content_text, timestamp, source_path, source_offset, event_hash
                ) VALUES (
                    :session_id, 'user', 'hello', CURRENT_TIMESTAMP, '/tmp/session.jsonl', 1, 'event-hash'
                )
                """
            ),
            {"session_id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO source_lines (
                    session_id, source_path, source_offset, branch_id, raw_json, line_hash
                ) VALUES (
                    :session_id, '/tmp/session.jsonl', 1, 1, '{"type":"user"}', 'line-hash'
                )
                """
            ),
            {"session_id": session_id},
        )

    # A second startup pass may do lightweight schema/index convergence, but it
    # must not stamp historical child rows.
    initialize_database(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT thread_id FROM events")).scalar() is None
        assert conn.execute(text("SELECT thread_id FROM source_lines")).scalar() is None

    plan = plan_heavy_migrations(engine)
    pending_names = {item.name for item in plan if item.pending}
    assert "20260521_session_identity_kernel_backfill" in pending_names

    run_items = apply_heavy_migrations(engine)
    assert any(
        item.name == "20260521_session_identity_kernel_backfill" and item.status == "applied"
        for item in run_items
    )

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT primary_thread_id FROM sessions WHERE id = :session_id"),
            {"session_id": session_id},
        ).scalar()
        assert conn.execute(text("SELECT thread_id FROM events")).scalar()
        assert conn.execute(text("SELECT thread_id FROM source_lines")).scalar()
        assert int(conn.execute(text("SELECT COUNT(*) FROM session_runs")).scalar() or 0) == 1


def test_apply_heavy_migrations_is_idempotent_and_records_ledger(tmp_path):
    db_path = tmp_path / "legacy_apply.db"
    engine = make_engine(f"sqlite:///{db_path}")
    _make_legacy_schema(engine)
    _migrate_agents_columns(engine)

    first_run = apply_heavy_migrations(engine)
    assert any(
        item.name == "20260304_events_branch_backfill" and item.status == "applied"
        for item in first_run
    )
    assert any(
        item.name == "20260304_source_lines_branch_revision_rebuild"
        and item.status == "applied"
        for item in first_run
    )

    pending_after_first = [item.name for item in plan_heavy_migrations(engine) if item.pending]
    assert pending_after_first == []

    second_run = apply_heavy_migrations(engine)
    assert all(item.status == "skipped" for item in second_run)

    with engine.connect() as conn:
        null_branch_rows = int(
            conn.execute(text("SELECT COUNT(*) FROM events WHERE branch_id IS NULL")).scalar() or 0
        )
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


def test_initialize_database_drops_legacy_file_reservations_table(tmp_path):
    db_path = tmp_path / "legacy_memory_cleanup.db"
    engine = make_engine(f"sqlite:///{db_path}")

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE file_reservations (
                id VARCHAR(36) PRIMARY KEY,
                file_path TEXT NOT NULL,
                project VARCHAR(255) NOT NULL DEFAULT '',
                agent VARCHAR(255) NOT NULL DEFAULT 'claude',
                reason TEXT,
                expires_at DATETIME NOT NULL,
                released_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                fiche_id INTEGER,
                content TEXT NOT NULL,
                type TEXT
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fiche_id INTEGER,
                title TEXT NOT NULL,
                active INTEGER,
                fiche_state JSON,
                memory_strategy TEXT,
                thread_type VARCHAR(20) NOT NULL DEFAULT 'chat',
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )

    initialize_database(engine)

    with engine.connect() as conn:
        file_reservations_exists = conn.execute(
            text(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'file_reservations'
                LIMIT 1
                """
            )
        ).fetchone()
        memories_exists = conn.execute(
            text(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'memories'
                LIMIT 1
                """
            )
        ).fetchone()
        thread_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(threads)"))}

    assert file_reservations_exists is None
    assert memories_exists is None
    assert "memory_strategy" not in thread_columns
