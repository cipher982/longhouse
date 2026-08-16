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


def _needs_provider_interaction_semantics_backfill(conn: Connection) -> tuple[bool, str]:
    if not _table_exists(conn, "events"):
        return False, "events table missing"
    columns = _table_columns(conn, "events")
    if "interaction_kind" not in columns or "title_eligible" not in columns or "interaction_context_key" not in columns:
        return False, "events semantic columns missing (run startup schema migration first)"
    null_rows = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events
                WHERE interaction_kind IS NULL OR title_eligible IS NULL
                """
            )
        ).scalar()
        or 0
    )
    context_rows = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events e
                JOIN sessions s ON s.id = e.session_id
                WHERE lower(s.provider) = 'claude'
                  AND e.interaction_context_key IS NULL
                  AND (
                      e.raw_json LIKE '%"promptId"%'
                      OR e.raw_json LIKE '%"uuid"%'
                      OR e.raw_json LIKE '%"parentUuid"%'
                      OR e.interaction_kind IN ('local_control', 'local_control_output', 'conversation_boundary')
                  )
                """
            )
        ).scalar()
        or 0
    )
    compressed_context_rows = 0
    if "raw_json_z" in columns:
        from zerg.services.raw_json_compression import decompress_raw_json

        compressed_rows = conn.execute(
            text(
                """
                SELECT e.raw_json_z, e.raw_json_codec, e.interaction_kind
                FROM events e
                JOIN sessions s ON s.id = e.session_id
                WHERE lower(s.provider) = 'claude'
                  AND e.interaction_context_key IS NULL
                  AND e.raw_json_z IS NOT NULL
                """
            )
        ).mappings()
        for row in compressed_rows:
            try:
                raw_value = json.loads(decompress_raw_json(row["raw_json_z"]))
            except Exception:
                # Keep a corrupt compressed row pending so the apply path
                # surfaces the durable decode error instead of claiming that
                # the migration is complete.
                compressed_context_rows += 1
                continue
            if isinstance(raw_value, dict) and (
                any(field in raw_value for field in ("promptId", "uuid", "parentUuid"))
                or row["interaction_kind"] in ("local_control", "local_control_output", "conversation_boundary")
            ):
                compressed_context_rows += 1
    context_rows += compressed_context_rows
    if null_rows <= 0 and context_rows <= 0:
        return False, "events semantic facts already populated"
    return True, f"events rows needing semantic replay={max(null_rows, context_rows)}"


def _apply_provider_interaction_semantics_backfill(conn: Connection) -> str:
    from zerg.services.provider_interaction_semantics import classify_provider_interaction
    from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
    from zerg.services.raw_json_compression import decompress_raw_json

    def seed_session_context(session_id: str, provider: str | None) -> dict[str, object]:
        if str(provider or "").strip().lower() != "claude":
            return {}
        raw_rows = conn.execute(
            text(
                """
                SELECT raw_json, raw_json_z, raw_json_codec
                FROM events
                WHERE session_id = :session_id AND role = 'user'
                ORDER BY timestamp, id
                """
            ),
            {"session_id": session_id},
        ).mappings()
        caveat_values: list[object] = []
        for raw_row in raw_rows:
            raw_value = raw_row["raw_json"]
            if int(raw_row["raw_json_codec"] or 0) == 1 and raw_row["raw_json_z"] is not None:
                raw_value = decompress_raw_json(raw_row["raw_json_z"])
            if isinstance(raw_value, str) and "<local-command-caveat>" in raw_value:
                caveat_values.append(raw_value)
        sequence_context: dict[str, object] = {}
        seed_provider_interaction_sequence_context("claude", caveat_values, sequence_context)
        return sequence_context

    result = conn.execute(
        text(
            """
            SELECT
                e.id,
                e.session_id,
                e.role,
                e.content_text,
                e.raw_json,
                e.raw_json_z,
                e.raw_json_codec,
                e.interaction_kind,
                e.title_eligible,
                e.interaction_context_key,
                s.provider
            FROM events e
            JOIN sessions s ON s.id = e.session_id
            ORDER BY e.session_id, e.timestamp, e.id
            """
        )
    )
    updated = 0
    current_session_id: str | None = None
    interaction_sequence_context: dict[str, object] = {}
    while rows := result.fetchmany(256):
        updates = []
        for row in rows:
            if str(row.session_id) != current_session_id:
                current_session_id = str(row.session_id)
                interaction_sequence_context = seed_session_context(current_session_id, row.provider)
            raw_json = row.raw_json
            if int(row.raw_json_codec or 0) == 1 and row.raw_json_z is not None:
                raw_json = decompress_raw_json(row.raw_json_z)
            rederive_claude_kind = str(row.provider or "").strip().lower() == "claude" and bool(str(raw_json or "").strip())
            semantics = classify_provider_interaction(
                row.provider,
                role=row.role,
                content_text=row.content_text,
                raw_json=raw_json,
                # Claude's raw envelope is the evidence needed to repair an
                # earlier false durable-user classification. Other providers
                # retain parser-owned normalized facts until their contract
                # provides equivalent replay evidence.
                interaction_kind=None if rederive_claude_kind else row.interaction_kind,
                sequence_context=interaction_sequence_context,
            )
            context_key = semantics["interaction_context_key"] or row.interaction_context_key
            if (
                rederive_claude_kind
                or row.interaction_kind is None
                or row.title_eligible is None
                or (
                    semantics["interaction_context_key"] is not None and row.interaction_context_key != semantics["interaction_context_key"]
                )
            ):
                updates.append(
                    {
                        "id": int(row.id),
                        "interaction_kind": semantics["interaction_kind"],
                        "title_eligible": int(bool(semantics["title_eligible"])),
                        "interaction_context_key": context_key,
                    }
                )
        if updates:
            conn.execute(
                text(
                    """
                    UPDATE events
                    SET interaction_kind = :interaction_kind,
                        title_eligible = :title_eligible,
                        interaction_context_key = :interaction_context_key
                    WHERE id = :id
                    """
                ),
                updates,
            )
        updated += len(updates)
    repaired, title_repaired = _repair_session_semantic_projections(conn)
    return f"updated_rows={updated}; repaired_sessions={repaired}; " f"repaired_titles={title_repaired}; claude_reclassification=v1"


def _apply_provider_interaction_semantics_reclassification(conn: Connection) -> str:
    """Replay Claude raw events for databases with the pre-replay migration."""

    details = _apply_provider_interaction_semantics_backfill(conn)
    return f"corrected_prior_backfill; {details}"


def _repair_session_semantic_projections(conn: Connection) -> tuple[int, int]:
    """Reconcile denormalized session reads after facts become authoritative.

    The semantic columns alone do not repair sessions ingested before this
    migration: counts, hot previews, timeline cards, and a title generated from
    a provider-local row can all already be stale. Recompute those projections
    in the same explicit migration transaction. A title is replaced only when
    its old first-message preview is provably one of the newly excluded local
    rows; otherwise existing user-visible title state is preserved.
    """

    from sqlalchemy import func
    from sqlalchemy import or_
    from sqlalchemy.orm import Session as OrmSession

    from zerg.models.agents import AgentEvent
    from zerg.models.agents import AgentSession
    from zerg.services.agents.store import AgentsStore
    from zerg.services.session_hot_cards import upsert_timeline_card_from_session
    from zerg.services.session_title import sanitize_timeline_title

    repaired = 0
    title_repaired = 0
    timeline_cards_available = _table_exists(conn, "timeline_cards")
    with OrmSession(
        bind=conn,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as session:
        store = AgentsStore(session)
        sessions = session.query(AgentSession).order_by(AgentSession.id.asc()).yield_per(256)
        for agent_session in sessions:
            head_branch_id = store.get_head_branch_id(agent_session.id)
            if head_branch_id is None:
                continue
            old_first_preview = str(agent_session.first_user_message_preview or "").strip()
            old_preview_was_local = bool(
                old_first_preview
                and session.query(AgentEvent.id)
                .filter(AgentEvent.session_id == agent_session.id)
                .filter(AgentEvent.branch_id == head_branch_id)
                .filter(AgentEvent.role == "user")
                .filter(
                    or_(
                        AgentEvent.content_text == old_first_preview,
                        func.substr(AgentEvent.content_text, 1, len(old_first_preview)) == old_first_preview,
                    )
                )
                .filter(AgentEvent.interaction_kind.in_(("local_control", "local_control_output", "conversation_boundary")))
                .first()
                is not None
            )

            store._sync_session_counts_to_head(agent_session.id, head_branch_id)
            new_first_preview = str(agent_session.first_user_message_preview or "").strip()
            if old_preview_was_local and new_first_preview != old_first_preview:
                # This is the precise stale-title case: the previous title
                # obligation was anchored to a row the semantic boundary just
                # removed. Replace it with the deterministic prompt fallback;
                # a future title worker may upgrade it to an AI title.
                agent_session.anchor_title = None
                agent_session.summary_title = sanitize_timeline_title(new_first_preview, max_words=6)
                agent_session.title_retry_at = None
                agent_session.title_last_error = None
                title_repaired += 1
            if timeline_cards_available:
                upsert_timeline_card_from_session(session, agent_session)
            repaired += 1
        session.commit()
    return repaired, title_repaired


def _migration_succeeded(conn: Connection, name: str) -> bool:
    if not _table_exists(conn, "migration_runs"):
        return False
    row = conn.execute(
        text("SELECT status FROM migration_runs WHERE migration_name = :name LIMIT 1"),
        {"name": name},
    ).fetchone()
    return row is not None and str(row[0]) == "succeeded"


def _migration_details(conn: Connection, name: str) -> str:
    if not _table_exists(conn, "migration_runs"):
        return ""
    row = conn.execute(
        text("SELECT details FROM migration_runs WHERE migration_name = :name LIMIT 1"),
        {"name": name},
    ).fetchone()
    return str(row[0] or "") if row is not None else ""


def _needs_provider_interaction_semantics_reclassification(conn: Connection) -> tuple[bool, str]:
    """Repair databases that ran the first semantic backfill before replay fix.

    The original migration name is retained for compatibility with its ledger
    entry. Its apply path now records a marker after doing sequence-aware
    Claude replay; an older successful entry lacks that marker and needs this
    one-time corrective pass.
    """

    name = "20260802_provider_interaction_semantics_reclassification"
    if _migration_succeeded(conn, name):
        return False, "provider interaction reclassification already applied"
    if not _migration_succeeded(conn, "20260801_provider_interaction_semantics_backfill"):
        return False, "initial provider interaction backfill is pending"
    if not _table_exists(conn, "events") or not _table_exists(conn, "sessions"):
        return False, "events/session tables missing"
    if "claude_reclassification=v1" in _migration_details(conn, "20260801_provider_interaction_semantics_backfill"):
        return False, "initial backfill already included Claude reclassification"
    raw_rows = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM events e
                JOIN sessions s ON s.id = e.session_id
                WHERE lower(s.provider) = 'claude'
                  AND (e.raw_json IS NOT NULL OR e.raw_json_z IS NOT NULL)
                """
            )
        ).scalar()
        or 0
    )
    if raw_rows <= 0:
        return False, "no Claude raw rows require reclassification"
    return True, f"Claude raw rows requiring reclassification={raw_rows}"


def _needs_provider_interaction_semantic_projection_repair(conn: Connection) -> tuple[bool, str]:
    name = "20260802_provider_interaction_semantic_projection_repair"
    if _migration_succeeded(conn, name):
        return False, "semantic session projections already repaired"
    if not _table_exists(conn, "sessions") or not _table_exists(conn, "events"):
        return False, "session/event tables missing"
    session_count = int(conn.execute(text("SELECT COUNT(*) FROM sessions")).scalar() or 0)
    if session_count <= 0:
        return False, "no sessions require projection repair"
    return True, f"sessions eligible for semantic projection repair={session_count}"


def _apply_provider_interaction_semantic_projection_repair(conn: Connection) -> str:
    repaired, title_repaired = _repair_session_semantic_projections(conn)
    return f"repaired_sessions={repaired}; repaired_titles={title_repaired}"


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
    _HeavyMigration(
        name="20260801_provider_interaction_semantics_backfill",
        description="Classify legacy provider-local interaction rows for semantic projections",
        needs=_needs_provider_interaction_semantics_backfill,
        apply=_apply_provider_interaction_semantics_backfill,
    ),
    _HeavyMigration(
        name="20260802_provider_interaction_semantics_reclassification",
        description="Replay Claude raw envelopes for databases with the pre-replay semantic backfill",
        needs=_needs_provider_interaction_semantics_reclassification,
        apply=_apply_provider_interaction_semantics_reclassification,
    ),
    _HeavyMigration(
        name="20260802_provider_interaction_semantic_projection_repair",
        description="Reconcile denormalized session projections after semantic classification",
        needs=_needs_provider_interaction_semantic_projection_repair,
        apply=_apply_provider_interaction_semantic_projection_repair,
    ),
)
