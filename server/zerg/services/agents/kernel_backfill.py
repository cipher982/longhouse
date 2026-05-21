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

    # Cheap early-out for converged DBs: no sessions are missing
    # primary_thread_id. (Aliases and per-session thread checks still need a
    # walk if pointers are set but a session lacks an alias — we rely on the
    # caller's idempotency for that, since it's the rarer fix-up path.)
    if (
        db.query(AgentSession.id)
        .filter(AgentSession.primary_thread_id.is_(None))
        .limit(1)
        .first()
        is None
    ):
        return {
            "sessions_seen": 0,
            "threads_created": 0,
            "primary_pointers_set": 0,
            "aliases_created": 0,
        }

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

    Idempotent: re-running on a fully-backfilled DB is a no-op. Includes a
    cheap early-out so a converged DB exits in O(tables) probes instead of
    O(sessions × tables) per-session updates.
    """

    counts: dict[str, int] = {model.__tablename__: 0 for model in _CHILD_THREAD_ID_TABLES}

    # Cheap early-out: if no child row anywhere has thread_id IS NULL, we're done.
    has_null = False
    for model in _CHILD_THREAD_ID_TABLES:
        if (
            db.query(model.thread_id)
            .filter(model.thread_id.is_(None))
            .limit(1)
            .first()
            is not None
        ):
            has_null = True
            break
    if not has_null:
        return counts

    primaries = dict(
        db.query(SessionThread.session_id, SessionThread.id)
        .filter(SessionThread.is_primary == 1)
        .all()
    )
    for model in _CHILD_THREAD_ID_TABLES:
        updated = 0
        for session_id, thread_id in primaries.items():
            stmt = (
                sql_update(model)
                .where(model.session_id == session_id, model.thread_id.is_(None))
                .values(thread_id=thread_id)
            )
            result = db.execute(stmt)
            updated += int(result.rowcount or 0)
        counts[model.__tablename__] = updated
    db.flush()
    return counts


def backfill_runs_and_connections(db: Session) -> dict[str, int]:
    """Synthesize one ``external_adopted`` run per primary thread that lacks
    one, plus a ``log_tail`` observe-only connection on the synthesized run.

    Phase 2 launchers create their own runs eagerly, so this only fills in
    history: pre-kernel sessions get a single run keyed to the primary thread.

    For ``run_id`` stamping on legacy ``SessionRuntimeState`` and
    ``SessionTurn`` rows, the **latest** run on the primary thread is used —
    a resumed session must land on the active run, not the original. Rows
    are filtered by ``thread_id == primary.id`` so subagent/branch threads
    keep their own run pointer.

    Launcher-owned runs are not touched and no connection is fabricated for
    them — those came in through Phase 2 dual-write paths and any missing
    connection there is a launcher bug, not a backfill concern.

    Idempotent: skips threads that already have any run row. Re-running over
    a converged DB is a no-op.
    """

    runs_created = 0
    connections_created = 0
    runtime_state_run_ids = 0
    turn_run_ids = 0

    # Cheap early-out for converged DBs: no primary threads missing a run
    # and no runtime/turn rows with run_id=NULL.
    threads_missing_run_subq = (
        db.query(SessionThread.id)
        .outerjoin(SessionRun, SessionRun.thread_id == SessionThread.id)
        .filter(SessionThread.is_primary == 1, SessionRun.id.is_(None))
        .limit(1)
        .first()
    )
    runtime_null = (
        db.query(SessionRuntimeState.runtime_key)
        .filter(SessionRuntimeState.run_id.is_(None))
        .limit(1)
        .first()
    )
    turn_null = (
        db.query(SessionTurn.id).filter(SessionTurn.run_id.is_(None)).limit(1).first()
    )
    if (
        threads_missing_run_subq is None
        and runtime_null is None
        and turn_null is None
    ):
        return {
            "runs_created": 0,
            "connections_created": 0,
            "runtime_state_run_ids": 0,
            "turn_run_ids": 0,
        }

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
            .order_by(SessionRun.started_at.desc(), SessionRun.id.desc())
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

            # Synthesize a log_tail connection only when we synthesized the
            # run. A launcher-owned run is responsible for its own connection.
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
        else:
            run = existing_run

        # Stamp run_id on runtime state / turns where NULL — but only on rows
        # already keyed to *this* primary thread. Rows pointing at a child or
        # branch thread keep their own (eventually-stamped) run pointer.
        result = db.execute(
            sql_update(SessionRuntimeState)
            .where(
                SessionRuntimeState.thread_id == thread.id,
                SessionRuntimeState.run_id.is_(None),
            )
            .values(run_id=run.id)
        )
        runtime_state_run_ids += int(result.rowcount or 0)

        result = db.execute(
            sql_update(SessionTurn)
            .where(
                SessionTurn.thread_id == thread.id,
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
