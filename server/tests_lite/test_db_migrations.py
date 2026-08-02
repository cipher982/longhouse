"""Tests for explicit heavy SQLite migration planning/runs."""

import json
import os
from datetime import datetime
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import _auto_add_missing_columns
from zerg.database import _migrate_agents_columns as _migrate_agents_columns_raw
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.db_migrations import apply_heavy_migrations
from zerg.db_migrations import ensure_migration_ledger
from zerg.db_migrations import plan_heavy_migrations
from zerg.models.agents import AgentSession
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest


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


def test_startup_migration_separates_session_closure_from_run_exit(tmp_path):
    db_path = tmp_path / "terminal_backfill.db"
    engine = make_engine(f"sqlite:///{db_path}")

    host_expired_id = "00000000-0000-0000-0000-000000000201"
    process_gone_id = "00000000-0000-0000-0000-000000000202"
    user_closed_id = "00000000-0000-0000-0000-000000000203"
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
                  (:process_gone_id, 'claude', 'test', '2026-05-04 17:00:00', 1, 1, 0),
                  (:user_closed_id, 'claude', 'test', '2026-05-04 17:00:00', 1, 1, 0)
                """
            ),
            {"host_expired_id": host_expired_id, "process_gone_id": process_gone_id, "user_closed_id": user_closed_id},
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
                  ),
                  (
                    'claude:user-closed', :user_closed_id, 'claude', 'finished', 'semantic',
                    '2026-05-04 17:50:00', '2026-05-04 17:50:00', '2026-05-04 17:50:00',
                    'user_closed', '2026-05-04 17:50:00'
                  )
                """
            ),
            {"host_expired_id": host_expired_id, "process_gone_id": process_gone_id, "user_closed_id": user_closed_id},
        )

    _migrate_agents_columns(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, ended_at, closed_at, close_reason FROM sessions ORDER BY id")).fetchall()

    assert rows == [
        (host_expired_id, None, None, None),
        (process_gone_id, None, None, None),
        (user_closed_id, None, "2026-05-04 17:50:00", "user_closed"),
    ]


def test_startup_migration_moves_terminal_runtime_evidence_to_run(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'run-terminal-backfill.db'}")
    initialize_database(engine)
    session_id = "00000000-0000-0000-0000-000000000211"
    thread_id = "00000000-0000-0000-0000-000000000212"
    run_id = "00000000-0000-0000-0000-000000000213"
    with engine.begin() as conn:
        # This migration only targets historical databases, which still carry
        # the retired execution_home adapter column used by earlier steps.
        conn.execute(text("ALTER TABLE sessions ADD COLUMN execution_home VARCHAR(32)"))
        conn.execute(
            text(
                """
                INSERT INTO sessions (id, provider, environment, started_at, user_messages, assistant_messages, tool_calls)
                VALUES (:session_id, 'codex', 'test', '2026-05-04 17:00:00', 1, 1, 0)
                """
            ),
            {"session_id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO session_threads (id, session_id, provider, branch_kind, is_primary)
                VALUES (:thread_id, :session_id, 'codex', 'root', 1)
                """
            ),
            {"thread_id": thread_id, "session_id": session_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO session_runs (id, thread_id, provider, launch_origin, started_at)
                VALUES (:run_id, :thread_id, 'codex', 'longhouse_spawned', '2026-05-04 17:00:00')
                """
            ),
            {"run_id": run_id, "thread_id": thread_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO session_runtime_state (
                    runtime_key, session_id, thread_id, run_id, provider, phase, phase_source,
                    timeline_anchor_at, terminal_state, terminal_at, runtime_version
                ) VALUES (
                    'codex:run-terminal', :session_id, :thread_id, :run_id, 'codex', 'finished', 'semantic',
                    '2026-05-04 17:40:00', 'process_gone', '2026-05-04 17:40:00', 1
                )
                """
            ),
            {"session_id": session_id, "thread_id": thread_id, "run_id": run_id},
        )

    _migrate_agents_columns(engine)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT ended_at, exit_status FROM session_runs WHERE id = :run_id"), {"run_id": run_id}).one()
        session_row = conn.execute(text("SELECT ended_at, closed_at FROM sessions WHERE id = :session_id"), {"session_id": session_id}).one()
    assert row == ("2026-05-04 17:40:00", "process_gone")
    assert session_row == (None, None)


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


def test_provider_interaction_semantics_backfill_classifies_legacy_claude_rows(tmp_path):
    db_path = tmp_path / "legacy_provider_semantics.db"
    engine = make_engine(f"sqlite:///{db_path}")
    _make_legacy_schema(engine)
    _migrate_agents_columns(engine)

    command = (
        "<command-name>/effort</command-name>\n"
        "            <command-message>effort</command-message>\n"
        "            <command-args>high</command-args>"
    )
    output = "<local-command-stdout>Set effort level to high</local-command-stdout>"
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO events (
                    session_id, role, content_text, timestamp, source_path, source_offset,
                    event_hash, raw_json
                ) VALUES (
                    :session_id, 'user', :caveat, CURRENT_TIMESTAMP, '/tmp/s.jsonl', 2,
                    'caveat-hash', :caveat_raw
                ), (
                    :session_id, 'user', :command, CURRENT_TIMESTAMP, '/tmp/s.jsonl', 3,
                    'command-hash', :command_raw
                ), (
                    :session_id, 'user', :output, CURRENT_TIMESTAMP, '/tmp/s.jsonl', 4,
                    'output-hash', :output_raw
                )
                """
            ),
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "caveat": "<local-command-caveat>local command</local-command-caveat>",
                "caveat_raw": json.dumps(
                    {
                        "type": "user",
                        "isMeta": True,
                        "promptId": "prompt-effort-1",
                        "message": {"role": "user", "content": "<local-command-caveat>local command</local-command-caveat>"},
                    }
                ),
                "command": command,
                "command_raw": json.dumps(
                    {"type": "user", "promptId": "prompt-effort-1", "message": {"role": "user", "content": command}}
                ),
                "output": output,
                "output_raw": json.dumps(
                    {"type": "user", "promptId": "prompt-effort-1", "message": {"role": "user", "content": output}}
                ),
            },
        )
        # Simulate a partial prior backfill: the caveat has facts, but its
        # same-prompt command/output siblings do not. The migration must replay
        # the complete session sequence so those siblings inherit context.
        conn.execute(
            text(
                """
                UPDATE events
                SET interaction_kind = 'local_control', title_eligible = 0
                WHERE session_id = :session_id AND source_offset = 2
                """
            ),
            {"session_id": "00000000-0000-0000-0000-000000000001"},
        )

    run_items = apply_heavy_migrations(engine)
    semantic_run = next(item for item in run_items if item.name == "20260801_provider_interaction_semantics_backfill")
    assert semantic_run.status == "applied"

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT content_text, interaction_kind, title_eligible, interaction_context_key
                FROM events
                WHERE session_id = :session_id
                ORDER BY id
                """
            ),
            {"session_id": "00000000-0000-0000-0000-000000000001"},
        ).fetchall()

    assert rows[0].interaction_kind == "durable_user_message"
    assert rows[0].title_eligible == 1
    assert rows[1].interaction_kind == "local_control"
    assert rows[1].title_eligible == 0
    assert rows[1].interaction_context_key == "prompt-effort-1"
    assert rows[2].interaction_kind == "local_control"
    assert rows[2].title_eligible == 0
    assert rows[2].interaction_context_key == "prompt-effort-1"
    assert rows[3].interaction_kind == "local_control_output"
    assert rows[3].title_eligible == 0
    assert rows[3].interaction_context_key == "prompt-effort-1"


def test_provider_interaction_reclassification_repairs_prior_successful_backfill(tmp_path):
    db_path = tmp_path / "prior_provider_semantics.db"
    engine = make_engine(f"sqlite:///{db_path}")
    _make_legacy_schema(engine)
    _migrate_agents_columns(engine)
    command = "<command-name>/effort</command-name><command-args>high</command-args>"
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE events
                SET content_text = :command,
                    raw_json = :raw_json,
                    interaction_kind = 'durable_user_message',
                    title_eligible = 1,
                    interaction_context_key = 'prompt-effort-1'
                WHERE id = 1
                """
            ),
            {
                "command": command,
                "raw_json": json.dumps(
                    {
                        "type": "user",
                        "isMeta": True,
                        "promptId": "prompt-effort-1",
                        "message": {"role": "user", "content": command},
                    }
                ),
            },
        )
    ensure_migration_ledger(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO migration_runs (migration_name, status, details, finished_at)
                VALUES (
                    '20260801_provider_interaction_semantics_backfill',
                    'succeeded',
                    'legacy prior run without sequence replay',
                    CURRENT_TIMESTAMP
                )
                """
            )
        )

    run_items = apply_heavy_migrations(engine)
    repair_run = next(
        item
        for item in run_items
        if item.name == "20260802_provider_interaction_semantics_reclassification"
    )
    assert repair_run.status == "applied"

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT interaction_kind, title_eligible, interaction_context_key
                FROM events
                WHERE id = 1
                """
            )
        ).one()
    assert row.interaction_kind == "local_control"
    assert row.title_eligible == 0
    assert row.interaction_context_key == "prompt-effort-1"


def test_provider_interaction_semantics_backfill_replays_reversed_claude_envelope(tmp_path):
    db_path = tmp_path / "reversed_provider_semantics.db"
    engine = make_engine(f"sqlite:///{db_path}")
    _make_legacy_schema(engine)
    _migrate_agents_columns(engine)
    prompt_id = "prompt-effort-reversed"
    command = "<command-name>/effort</command-name>"
    caveat = "<local-command-caveat>native</local-command-caveat>"
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE events
                SET role = 'user', content_text = :command, raw_json = :command_raw
                WHERE id = 1
                """
            ),
            {
                "command": command,
                "command_raw": json.dumps(
                    {
                        "type": "user",
                        "promptId": prompt_id,
                        "message": {"role": "user", "content": command},
                    }
                ),
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO events (
                    session_id, role, content_text, timestamp, source_path, source_offset,
                    event_hash, raw_json
                ) VALUES (
                    :session_id, 'user', :caveat, CURRENT_TIMESTAMP, '/tmp/s.jsonl', 2,
                    'reversed-caveat-hash', :caveat_raw
                )
                """
            ),
            {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "caveat": caveat,
                "caveat_raw": json.dumps(
                    {
                        "type": "user",
                        "isMeta": True,
                        "promptId": prompt_id,
                        "message": {"role": "user", "content": caveat},
                    }
                ),
            },
        )

    run_items = apply_heavy_migrations(engine)
    semantic_run = next(item for item in run_items if item.name == "20260801_provider_interaction_semantics_backfill")
    assert semantic_run.status == "applied"

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT interaction_kind, title_eligible, interaction_context_key
                FROM events
                ORDER BY id
                """
            )
        ).fetchall()
    assert [row.interaction_kind for row in rows] == ["local_control", "local_control"]
    assert [row.title_eligible for row in rows] == [0, 0]
    assert [row.interaction_context_key for row in rows] == [prompt_id, prompt_id]


def test_projection_repair_handles_preclassified_stale_title(tmp_path):
    db_path = tmp_path / "preclassified_provider_semantics.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    store = AgentsStore(db)
    session_id = uuid4()
    timestamp = datetime(2026, 8, 1, tzinfo=timezone.utc)
    caveat = "<local-command-caveat>native local command</local-command-caveat>"
    command = "<command-name>/effort</command-name><command-args>high</command-args>"
    output = "<local-command-stdout>Set effort level to high</local-command-stdout>"
    prompt = "Build the feature"

    def event(content: str, offset: int, *, is_meta: bool = False) -> EventIngest:
        row = {
            "type": "user",
            "promptId": "prompt-effort-1",
            "message": {"role": "user", "content": content},
        }
        if is_meta:
            row["isMeta"] = True
        return EventIngest(
            role="user",
            content_text=content,
            raw_json=json.dumps(row),
            timestamp=timestamp,
            source_path="/claude/session.jsonl",
            source_offset=offset,
        )

    with patch("zerg.services.session_title_trigger.maybe_start_initial_title_generation"):
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="longhouse",
                device_id="cinder",
                cwd="/tmp/longhouse",
                started_at=timestamp,
                events=[event(caveat, 0, is_meta=True), event(command, 1), event(output, 2), event(prompt, 3)],
            )
        )

    session = db.query(AgentSession).filter_by(id=session_id).one()
    session.first_user_message_preview = command
    session.summary_title = "Effort level settings"
    session.anchor_title = "Effort level settings"
    db.commit()

    run_items = apply_heavy_migrations(engine)
    repair_run = next(
        item for item in run_items if item.name == "20260802_provider_interaction_semantic_projection_repair"
    )
    assert repair_run.status == "applied"

    db.expire_all()
    repaired = db.query(AgentSession).filter_by(id=session_id).one()
    assert repaired.user_messages == 1
    assert repaired.first_user_message_preview == prompt
    assert repaired.summary_title == "Build the feature"
    assert repaired.anchor_title is None
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
        ("20260801_provider_interaction_semantics_backfill", "succeeded"),
        ("20260802_provider_interaction_semantic_projection_repair", "succeeded"),
    ]

    normalized_sql = "".join(ch for ch in _table_sql(engine, "source_lines").lower() if not ch.isspace())
    assert "unique(session_id,source_path,source_offset)" not in normalized_sql
