"""Idempotent backfill helpers for the session identity kernel.

Phase 1 created a root thread per session. Phase 3 stamps ``thread_id`` on
every legacy child row that still has it NULL, and synthesizes a single
``external_adopted`` run + connection per session so the kernel has a
complete view of historical sessions. Live launchers continue to write
their own runs/connections.

This module is purely additive — it never deletes or rewrites legacy rows
and never displaces a launcher-owned run.

See docs/specs/session-identity-kernel.md.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.models.agents import SessionTurn


def backfill_root_threads(db: Session) -> dict[str, int]:
    """Ensure every AgentSession has a primary thread row.

    Idempotent: re-running on the same database produces the same result with
    no duplicate rows. Order-independent: the function may be called repeatedly
    or interleaved with new session creation without producing inconsistent
    state.

    Returns counts: {sessions_seen, threads_created, primary_pointers_set,
    aliases_created}.
    """

    sessions_seen = 0
    threads_created = 0
    primary_pointers_set = 0
    aliases_created = 0

    sessions = db.query(AgentSession).all()
    for session in sessions:
        sessions_seen += 1

        thread = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == session.id, SessionThread.is_primary == 1)
            .one_or_none()
        )
        if thread is None:
            thread = SessionThread(
                session_id=session.id,
                provider=session.provider,
                branch_kind="root",
                is_primary=1,
            )
            db.add(thread)
            db.flush()
            threads_created += 1

        if session.primary_thread_id != thread.id:
            session.primary_thread_id = thread.id
            primary_pointers_set += 1

        # If the legacy AgentSession carries a provider_session_id, mirror it
        # as an alias so Phase 2/4 resolver paths can find the thread without
        # consulting AgentSession directly.
        legacy_provider_id = getattr(session, "provider_session_id", None)
        if legacy_provider_id:
            existing = (
                db.query(SessionThreadAlias)
                .filter(
                    SessionThreadAlias.thread_id == thread.id,
                    SessionThreadAlias.provider == session.provider,
                    SessionThreadAlias.alias_kind == "provider_session_id",
                    SessionThreadAlias.alias_value == legacy_provider_id,
                )
                .one_or_none()
            )
            if existing is None:
                db.add(
                    SessionThreadAlias(
                        thread_id=thread.id,
                        provider=session.provider,
                        alias_kind="provider_session_id",
                        alias_value=legacy_provider_id,
                    )
                )
                aliases_created += 1

    db.flush()
    return {
        "sessions_seen": sessions_seen,
        "threads_created": threads_created,
        "primary_pointers_set": primary_pointers_set,
        "aliases_created": aliases_created,
    }


_CHILD_THREAD_ID_TABLES = (
    AgentEvent,
    AgentSourceLine,
    SessionObservation,
    SessionTurn,
    SessionInput,
    SessionRuntimeState,
)


def backfill_child_thread_ids(db: Session) -> dict[str, int]:
    """Stamp thread_id on every legacy child row that's still NULL.

    Each child table has a ``session_id`` column; the backfill resolves the
    primary thread per session and bulk-updates rows whose thread_id is
    currently NULL. Rows that already carry a thread_id are never touched.

    Idempotent: re-running on a fully-backfilled DB is a no-op.
    """

    counts: dict[str, int] = {}

    primaries = dict(
        db.query(SessionThread.session_id, SessionThread.id)
        .filter(SessionThread.is_primary == 1)
        .all()
    )
    for model in _CHILD_THREAD_ID_TABLES:
        table_name = model.__tablename__
        updated = 0
        for session_id, thread_id in primaries.items():
            stmt = (
                sql_update(model)
                .where(model.session_id == session_id, model.thread_id.is_(None))
                .values(thread_id=thread_id)
            )
            result = db.execute(stmt)
            updated += int(result.rowcount or 0)
        counts[table_name] = updated
    db.flush()
    return counts


def backfill_runs_and_connections(db: Session) -> dict[str, int]:
    """Synthesize one ``external_adopted`` run + connection per session.

    Phase 2 launchers create their own runs eagerly, so this only fills in
    history: sessions that pre-date the kernel get a single run keyed to
    the primary thread, plus a ``log_tail`` observe-only connection.

    Idempotent: skips threads that already have any run row.
    """

    runs_created = 0
    connections_created = 0
    runtime_state_run_ids = 0
    turn_run_ids = 0

    threads = (
        db.query(SessionThread)
        .filter(SessionThread.is_primary == 1)
        .all()
    )
    now = datetime.now(timezone.utc)

    for thread in threads:
        existing_run = (
            db.query(SessionRun)
            .filter(SessionRun.thread_id == thread.id)
            .order_by(SessionRun.started_at.asc(), SessionRun.id.asc())
            .first()
        )
        if existing_run is None:
            session = (
                db.query(AgentSession)
                .filter(AgentSession.id == thread.session_id)
                .first()
            )
            if session is None:
                continue
            run = SessionRun(
                thread_id=thread.id,
                provider=thread.provider or session.provider,
                host_id=getattr(session, "device_id", None),
                cwd=getattr(session, "cwd", None),
                launch_origin="external_adopted",
                started_at=getattr(session, "started_at", None) or now,
                ended_at=getattr(session, "ended_at", None),
            )
            db.add(run)
            db.flush()
            runs_created += 1
        else:
            run = existing_run

        existing_conn = (
            db.query(SessionConnection)
            .filter(SessionConnection.run_id == run.id)
            .first()
        )
        if existing_conn is None:
            db.add(
                SessionConnection(
                    run_id=run.id,
                    control_plane="log_tail",
                    acquisition_kind="observe_only",
                    state="ended" if run.ended_at is not None else "attached",
                    can_send_input=0,
                    can_interrupt=0,
                    can_terminate=0,
                    can_tail_output=1,
                    can_resume=0,
                )
            )
            connections_created += 1

        # Stamp run_id on runtime state / turns if NULL.
        result = db.execute(
            sql_update(SessionRuntimeState)
            .where(
                SessionRuntimeState.session_id == thread.session_id,
                SessionRuntimeState.run_id.is_(None),
            )
            .values(run_id=run.id)
        )
        runtime_state_run_ids += int(result.rowcount or 0)

        result = db.execute(
            sql_update(SessionTurn)
            .where(
                SessionTurn.session_id == thread.session_id,
                SessionTurn.run_id.is_(None),
            )
            .values(run_id=run.id)
        )
        turn_run_ids += int(result.rowcount or 0)

    db.flush()
    return {
        "runs_created": runs_created,
        "connections_created": connections_created,
        "runtime_state_run_ids": runtime_state_run_ids,
        "turn_run_ids": turn_run_ids,
    }


def backfill_session_identity_kernel(db: Session) -> dict[str, dict[str, int]]:
    """Run the three-stage backfill in dependency order.

    1. ``backfill_root_threads`` — primary thread + provider_session_id alias.
    2. ``backfill_child_thread_ids`` — stamp thread_id on every legacy child row.
    3. ``backfill_runs_and_connections`` — synthesize one observe-only run +
       connection per session for sessions without launcher-owned runs.

    Idempotent end-to-end. Safe to run on every startup or as a one-shot CLI.
    """

    return {
        "threads": backfill_root_threads(db),
        "children": backfill_child_thread_ids(db),
        "runs": backfill_runs_and_connections(db),
    }
