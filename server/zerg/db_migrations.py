"""Explicit heavy SQLite migrations with ledger + idempotent runner.

Startup (`initialize_database`) must stay fast and only handle lightweight schema
drift. Expensive data rewrites live here and run explicitly via:

    longhouse migrate --apply
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import Engine
from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class MigrationPlanItem:
    name: str
    description: str
    pending: bool
    reason: str
    last_status: str | None = None


@dataclass(frozen=True)
class MigrationRunItem:
    name: str
    status: str  # applied | skipped | failed
    details: str | None = None


@dataclass(frozen=True)
class _HeavyMigration:
    name: str
    description: str
    needs: Callable[[Connection], tuple[bool, str]]
    apply: Callable[[Connection], str | None]


def ensure_migration_ledger(engine: Engine) -> None:
    """Ensure the migration ledger exists."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS migration_runs (
                    migration_name TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at DATETIME,
                    details TEXT
                )
                """
            )
        )


def plan_heavy_migrations(engine: Engine) -> list[MigrationPlanItem]:
    """Return heavy migration plan without applying changes."""
    if engine.dialect.name != "sqlite":
        return []

    ensure_migration_ledger(engine)
    with engine.connect() as conn:
        status_map = _migration_status_map(conn)
        plan: list[MigrationPlanItem] = []
        for migration in _HEAVY_MIGRATIONS:
            pending, reason = migration.needs(conn)
            plan.append(
                MigrationPlanItem(
                    name=migration.name,
                    description=migration.description,
                    pending=pending,
                    reason=reason,
                    last_status=status_map.get(migration.name),
                )
            )
        return plan


def apply_heavy_migrations(engine: Engine) -> list[MigrationRunItem]:
    """Apply pending heavy migrations, recording run status in the ledger."""
    if engine.dialect.name != "sqlite":
        return []

    ensure_migration_ledger(engine)
    results: list[MigrationRunItem] = []

    for migration in _HEAVY_MIGRATIONS:
        with engine.connect() as conn:
            pending, reason = migration.needs(conn)
        if not pending:
            results.append(MigrationRunItem(name=migration.name, status="skipped", details=reason))
            continue

        try:
            with engine.begin() as conn:
                _record_migration_status(conn, migration.name, "running", reason)
                details = migration.apply(conn) or "ok"
                _record_migration_status(conn, migration.name, "succeeded", details)
            results.append(MigrationRunItem(name=migration.name, status="applied", details=details))
        except Exception as exc:
            with engine.begin() as conn:
                _record_migration_status(conn, migration.name, "failed", str(exc))
            results.append(MigrationRunItem(name=migration.name, status="failed", details=str(exc)))
            raise

    return results


def pending_heavy_migration_names(engine: Engine) -> list[str]:
    """Return names of pending heavy migrations."""
    return [item.name for item in plan_heavy_migrations(engine) if item.pending]


def _migration_status_map(conn: Connection) -> dict[str, str]:
    rows = conn.execute(text("SELECT migration_name, status FROM migration_runs")).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _record_migration_status(conn: Connection, name: str, status: str, details: str | None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO migration_runs (
                migration_name,
                status,
                started_at,
                finished_at,
                details
            )
            VALUES (
                :name,
                :status,
                CURRENT_TIMESTAMP,
                CASE WHEN :status = 'running' THEN NULL ELSE CURRENT_TIMESTAMP END,
                :details
            )
            ON CONFLICT(migration_name) DO UPDATE SET
                status = excluded.status,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                details = excluded.details
            """
        ),
        {"name": name, "status": status, "details": details},
    )


def _table_exists(conn: Connection, table_name: str) -> bool:
    row = conn.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
            LIMIT 1
            """
        ),
        {"table_name": table_name},
    ).fetchone()
    return row is not None


def _table_columns(conn: Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {str(row[1]) for row in rows}


def _table_sql(conn: Connection, table_name: str) -> str:
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


def _normalize_sql(sql: str) -> str:
    return "".join(ch for ch in sql.lower() if not ch.isspace() and ch not in {'"', "`", "[", "]"})


def _needs_events_branch_backfill(conn: Connection) -> tuple[bool, str]:
    columns = _table_columns(conn, "events")
    if not columns:
        return False, "events table missing"
    if "branch_id" not in columns:
        return False, "events.branch_id missing (run startup schema migration first)"
    null_rows = int(conn.execute(text("SELECT COUNT(*) FROM events WHERE branch_id IS NULL")).scalar() or 0)
    if null_rows <= 0:
        return False, "events.branch_id already populated"
    return True, f"events rows with NULL branch_id={null_rows}"


def _apply_events_branch_backfill(conn: Connection) -> str:
    conn.execute(
        text(
            """
            WITH branch_choice AS (
                SELECT
                    session_id,
                    COALESCE(MAX(CASE WHEN is_head = 1 THEN id END), MAX(id)) AS branch_id
                FROM session_branches
                GROUP BY session_id
            )
            UPDATE events
            SET branch_id = (
                SELECT bc.branch_id
                FROM branch_choice bc
                WHERE bc.session_id = events.session_id
            )
            WHERE branch_id IS NULL
            """
        )
    )
    changed = int(conn.execute(text("SELECT changes()")).scalar() or 0)
    return f"updated_rows={changed}"


def _needs_source_lines_rebuild(conn: Connection) -> tuple[bool, str]:
    if not _table_exists(conn, "source_lines"):
        return False, "source_lines table missing"

    columns = _table_columns(conn, "source_lines")
    missing = [col for col in ("branch_id", "revision") if col not in columns]
    table_sql = _normalize_sql(_table_sql(conn, "source_lines"))
    has_legacy_unique = "unique(session_id,source_path,source_offset)" in table_sql

    reasons: list[str] = []
    if missing:
        reasons.append(f"missing_columns={','.join(missing)}")
    if has_legacy_unique:
        reasons.append("legacy_unique_constraint=session_id,source_path,source_offset")
    if not reasons:
        return False, "source_lines schema already branch/revision aware"
    return True, "; ".join(reasons)


def _apply_source_lines_rebuild(conn: Connection) -> str:
    columns = _table_columns(conn, "source_lines")
    if "line_hash" not in columns:
        raise RuntimeError("source_lines.line_hash missing; cannot rebuild deterministically")

    branch_expr = "COALESCE(sl.branch_id, bc.branch_id, 1)" if "branch_id" in columns else "COALESCE(bc.branch_id, 1)"
    revision_expr = "COALESCE(sl.revision, 1)" if "revision" in columns else "1"
    copy_expr = "COALESCE(sl.is_branch_copy, 0)" if "is_branch_copy" in columns else "0"

    conn.execute(text("DROP TABLE IF EXISTS source_lines_new"))
    conn.execute(
        text(
            """
            CREATE TABLE source_lines_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id CHAR(36) NOT NULL,
                source_path TEXT NOT NULL,
                source_offset BIGINT NOT NULL,
                branch_id INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                is_branch_copy INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                raw_json_z BLOB,
                raw_json_codec INTEGER NOT NULL DEFAULT 0,
                line_hash VARCHAR(64) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            WITH branch_choice AS (
                SELECT
                    session_id,
                    COALESCE(MAX(CASE WHEN is_head = 1 THEN id END), MAX(id), 1) AS branch_id
                FROM session_branches
                GROUP BY session_id
            )
            INSERT INTO source_lines_new (
                id,
                session_id,
                source_path,
                source_offset,
                branch_id,
                revision,
                is_branch_copy,
                raw_json,
                raw_json_z,
                raw_json_codec,
                line_hash,
                created_at
            )
            SELECT
                sl.id,
                sl.session_id,
                sl.source_path,
                sl.source_offset,
                {branch_expr} AS branch_id,
                {revision_expr} AS revision,
                {copy_expr} AS is_branch_copy,
                sl.raw_json,
                sl.raw_json_z,
                COALESCE(sl.raw_json_codec, 0),
                sl.line_hash,
                COALESCE(sl.created_at, CURRENT_TIMESTAMP)
            FROM source_lines sl
            LEFT JOIN branch_choice bc ON bc.session_id = sl.session_id
            """
        )
    )
    copied_rows = int(conn.execute(text("SELECT COUNT(*) FROM source_lines_new")).scalar() or 0)

    conn.execute(text("DROP TABLE source_lines"))
    conn.execute(text("ALTER TABLE source_lines_new RENAME TO source_lines"))
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_source_line_revision
            ON source_lines(session_id, branch_id, source_path, source_offset, revision)
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_source_line_hash
            ON source_lines(session_id, branch_id, source_path, source_offset, line_hash)
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_source_lines_session_offset
            ON source_lines(session_id, branch_id, source_offset)
            """
        )
    )
    return f"copied_rows={copied_rows}"


_IDENTITY_CHILD_THREAD_TABLES: tuple[str, ...] = (
    "events",
    "source_lines",
    "session_observations",
    "session_turns",
    "session_inputs",
    "session_runtime_state",
)


def _has_null_value(conn: Connection, table_name: str, column_name: str) -> bool:
    if not _table_exists(conn, table_name):
        return False
    if column_name not in _table_columns(conn, table_name):
        return False
    row = conn.execute(text(f"SELECT 1 FROM {table_name} WHERE {column_name} IS NULL LIMIT 1")).fetchone()
    return row is not None


def _needs_session_identity_kernel_backfill(conn: Connection) -> tuple[bool, str]:
    if not _table_exists(conn, "sessions"):
        return False, "sessions table missing"
    for table_name in ("session_threads", "session_runs", "session_connections"):
        if not _table_exists(conn, table_name):
            return False, f"{table_name} table missing (run startup schema migration first)"

    reasons: list[str] = []
    if _has_null_value(conn, "sessions", "primary_thread_id"):
        reasons.append("sessions.primary_thread_id has NULL rows")

    for table_name in _IDENTITY_CHILD_THREAD_TABLES:
        if _has_null_value(conn, table_name, "thread_id"):
            reasons.append(f"{table_name}.thread_id has NULL rows")

    if _has_null_value(conn, "session_runtime_state", "run_id"):
        reasons.append("session_runtime_state.run_id has NULL rows")
    if _has_null_value(conn, "session_turns", "run_id"):
        reasons.append("session_turns.run_id has NULL rows")

    if _table_exists(conn, "source_lines") and _table_exists(conn, "events"):
        leaked_source_line = conn.execute(
            text(
                """
                SELECT 1
                FROM source_lines
                WHERE source_path LIKE '%/subagents/%'
                  AND instr(source_path, CAST(session_id AS TEXT) || '/subagents/') = 0
                LIMIT 1
                """
            )
        ).fetchone()
        leaked_event = conn.execute(
            text(
                """
                SELECT 1
                FROM events
                WHERE source_path LIKE '%/subagents/%'
                  AND instr(source_path, CAST(session_id AS TEXT) || '/subagents/') = 0
                LIMIT 1
                """
            )
        ).fetchone()
        if leaked_source_line is not None or leaked_event is not None:
            reasons.append("provider subagent transcript rows are stored as standalone sessions")

    threads_missing_run = conn.execute(
        text(
            """
            SELECT 1
            FROM session_threads t
            LEFT JOIN session_runs r ON r.thread_id = t.id
            WHERE t.is_primary = 1
              AND r.id IS NULL
            LIMIT 1
            """
        )
    ).fetchone()
    if threads_missing_run is not None:
        reasons.append("primary session_threads missing session_runs")

    if not reasons:
        return False, "session identity kernel already backfilled"
    return True, "; ".join(reasons)


def _apply_session_identity_kernel_backfill(conn: Connection) -> str:
    from sqlalchemy.orm import Session as OrmSession

    from zerg.services.agents.kernel_backfill import backfill_session_identity_kernel

    with OrmSession(
        bind=conn,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as session:
        report = backfill_session_identity_kernel(session)
        session.commit()
    return json.dumps(report, sort_keys=True)


_HEAVY_MIGRATIONS: tuple[_HeavyMigration, ...] = (
    _HeavyMigration(
        name="20260304_events_branch_backfill",
        description="Populate legacy events.branch_id values",
        needs=_needs_events_branch_backfill,
        apply=_apply_events_branch_backfill,
    ),
    _HeavyMigration(
        name="20260304_source_lines_branch_revision_rebuild",
        description="Rebuild legacy source_lines schema for branch/revision-aware replay",
        needs=_needs_source_lines_rebuild,
        apply=_apply_source_lines_rebuild,
    ),
    _HeavyMigration(
        name="20260521_session_identity_kernel_backfill",
        description="Stamp legacy sessions/events/source lines/observations with identity-kernel thread/run ids",
        needs=_needs_session_identity_kernel_backfill,
        apply=_apply_session_identity_kernel_backfill,
    ),
)
