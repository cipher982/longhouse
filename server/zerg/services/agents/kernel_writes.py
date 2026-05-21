"""Helpers for Phase 2 write-path migration to the session identity kernel.

These helpers materialize the four-noun identity (session/thread/run/
connection) on write and are intended to be called from existing managed
launch, ingest, and bridge state paths. They are idempotent and safe to
call from any code that already touches the legacy ``AgentSession`` row.

Phase 2 is a *dual-write* phase: the legacy columns (``provider_session_id``,
``managed_transport``, ``execution_home``, etc.) are still authoritative for
reads. Phase 3 flips reads to the kernel and drops the legacy columns.

See docs/specs/session-identity-kernel.md.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias


def ensure_primary_thread(db: Session, session: AgentSession) -> SessionThread:
    """Return (creating if needed) the primary thread for ``session``.

    Idempotent: callers may invoke this on every write path that touches
    a session. It also keeps ``session.primary_thread_id`` pointed at the
    returned thread.

    Caller is responsible for ``db.commit()`` / ``db.flush()``.
    """

    thread = (
        db.query(SessionThread)
        .filter(
            SessionThread.session_id == session.id,
            SessionThread.is_primary == 1,
        )
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

    if session.primary_thread_id != thread.id:
        session.primary_thread_id = thread.id

    return thread


def record_thread_alias(
    db: Session,
    *,
    thread: SessionThread,
    provider: str,
    alias_kind: str,
    alias_value: str,
) -> None:
    """Record ``alias_value`` as evidence pointing to ``thread`` if not present.

    Aliases are evidence, not identity: re-recording the same alias for
    the same thread is a no-op (per-thread uniqueness constraint).

    Caller is responsible for ``db.commit()`` / ``db.flush()``.
    """

    if not alias_value:
        return

    existing = (
        db.query(SessionThreadAlias)
        .filter(
            SessionThreadAlias.thread_id == thread.id,
            SessionThreadAlias.provider == provider,
            SessionThreadAlias.alias_kind == alias_kind,
            SessionThreadAlias.alias_value == alias_value,
        )
        .one_or_none()
    )
    if existing is not None:
        return

    db.add(
        SessionThreadAlias(
            thread_id=thread.id,
            provider=provider,
            alias_kind=alias_kind,
            alias_value=alias_value,
        )
    )


def resolve_thread_id_for_session(db: Session, session_id) -> Optional[str]:
    """Cheap lookup: thread.id for the session's primary thread, or None.

    Used by ingest reducers that need to stamp ``thread_id`` on a new row
    but only have ``session_id`` in hand. Returns None when no primary
    thread has been materialized yet (e.g. a brand-new session in the
    same transaction before ``ensure_primary_thread`` ran).
    """

    row = (
        db.query(SessionThread.id)
        .filter(
            SessionThread.session_id == session_id,
            SessionThread.is_primary == 1,
        )
        .one_or_none()
    )
    return row[0] if row is not None else None


def record_launch_attempt(
    db: Session,
    *,
    session: AgentSession,
    thread: SessionThread | None,
    provider: str,
    host_id: str | None,
    client_request_id: str | None = None,
    command_id: str | None = None,
    state: str = "pending",
    expires_at: datetime | None = None,
) -> SessionLaunchAttempt:
    """Idempotent insert keyed by (session_id, client_request_id) when both provided.

    Returns the existing or newly-created attempt. Caller manages commit.
    """

    if client_request_id:
        existing = (
            db.query(SessionLaunchAttempt)
            .filter(
                SessionLaunchAttempt.session_id == session.id,
                SessionLaunchAttempt.client_request_id == client_request_id,
            )
            .one_or_none()
        )
        if existing is not None:
            return existing

    attempt = SessionLaunchAttempt(
        session_id=session.id,
        thread_id=thread.id if thread is not None else None,
        provider=provider,
        host_id=host_id,
        client_request_id=client_request_id,
        command_id=command_id,
        state=state,
        expires_at=expires_at,
    )
    db.add(attempt)
    db.flush()
    return attempt


def update_launch_attempt(
    db: Session,
    attempt: SessionLaunchAttempt,
    *,
    state: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    run: SessionRun | None = None,
    expires_at: datetime | None = None,
    clear_expires: bool = False,
) -> None:
    if state is not None:
        attempt.state = state
    if error_code is not None:
        attempt.error_code = error_code
    if error_message is not None:
        attempt.error_message = error_message
    if run is not None:
        attempt.run_id = run.id
    if clear_expires:
        attempt.expires_at = None
    elif expires_at is not None:
        attempt.expires_at = expires_at
    attempt.updated_at = datetime.now(timezone.utc)


def record_run(
    db: Session,
    *,
    thread: SessionThread,
    provider: str,
    host_id: str | None,
    boot_id: str | None = None,
    pid: int | None = None,
    process_start_time: datetime | None = None,
    cwd: str | None = None,
    launch_origin: str = "longhouse_spawned",
    started_at: datetime | None = None,
) -> SessionRun:
    """Insert a new SessionRun row. Caller manages commit."""

    run = SessionRun(
        thread_id=thread.id,
        provider=provider,
        host_id=host_id,
        boot_id=boot_id,
        pid=pid,
        process_start_time=process_start_time,
        cwd=cwd,
        launch_origin=launch_origin,
        started_at=started_at or datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def ensure_open_run_for_session(
    db: Session,
    session: AgentSession,
    *,
    launch_origin: str = "external_adopted",
    host_id: str | None = None,
) -> SessionRun:
    """Return the most recent open run for the session's primary thread.

    Creates an ``external_adopted`` run if none exists. Used by paths that
    observe live state (heartbeats, lease snapshots) on sessions that may
    not have come through a Longhouse launcher.

    Caller is responsible for ``db.commit()``.
    """

    thread = ensure_primary_thread(db, session)
    open_run = (
        db.query(SessionRun)
        .filter(SessionRun.thread_id == thread.id)
        .filter(SessionRun.ended_at.is_(None))
        .order_by(SessionRun.started_at.desc())
        .first()
    )
    if open_run is not None:
        return open_run
    return record_run(
        db,
        thread=thread,
        provider=session.provider,
        host_id=host_id or getattr(session, "device_id", None),
        cwd=getattr(session, "cwd", None),
        launch_origin=launch_origin,
    )


def upsert_connection_for_run(
    db: Session,
    *,
    run: SessionRun,
    control_plane: str,
    acquisition_kind: str,
    state: str,
    external_name: str | None = None,
    can_send_input: int | None = None,
    can_interrupt: int | None = None,
    can_terminate: int | None = None,
    can_tail_output: int | None = None,
    can_resume: int | None = None,
) -> SessionConnection:
    """Upsert one connection per (run, control_plane).

    Capability flags are only updated when a non-None value is supplied so
    callers that only know about state can leave capability bits alone.
    Caller is responsible for ``db.commit()``.
    """

    existing = (
        db.query(SessionConnection)
        .filter(
            SessionConnection.run_id == run.id,
            SessionConnection.control_plane == control_plane,
        )
        .order_by(SessionConnection.id.desc())
        .first()
    )
    now = datetime.now(timezone.utc)
    if existing is None:
        return record_connection(
            db,
            run=run,
            control_plane=control_plane,
            acquisition_kind=acquisition_kind,
            state=state,
            external_name=external_name,
            can_send_input=can_send_input or 0,
            can_interrupt=can_interrupt or 0,
            can_terminate=can_terminate or 0,
            can_tail_output=can_tail_output or 0,
            can_resume=can_resume or 0,
        )

    if existing.state != state:
        existing.state = state
        if state in {"detached", "released", "ended"}:
            existing.released_at = now
        elif state == "attached":
            existing.released_at = None
    if external_name is not None and existing.external_name != external_name:
        existing.external_name = external_name
    if can_send_input is not None:
        existing.can_send_input = can_send_input
    if can_interrupt is not None:
        existing.can_interrupt = can_interrupt
    if can_terminate is not None:
        existing.can_terminate = can_terminate
    if can_tail_output is not None:
        existing.can_tail_output = can_tail_output
    if can_resume is not None:
        existing.can_resume = can_resume
    existing.last_health_at = now
    return existing


def record_connection(
    db: Session,
    *,
    run: SessionRun,
    control_plane: str,
    acquisition_kind: str,
    state: str = "attached",
    external_name: str | None = None,
    can_send_input: int = 0,
    can_interrupt: int = 0,
    can_terminate: int = 0,
    can_tail_output: int = 0,
    can_resume: int = 0,
) -> SessionConnection:
    """Insert a new SessionConnection row. Caller manages commit."""

    conn = SessionConnection(
        run_id=run.id,
        control_plane=control_plane,
        acquisition_kind=acquisition_kind,
        state=state,
        external_name=external_name,
        can_send_input=can_send_input,
        can_interrupt=can_interrupt,
        can_terminate=can_terminate,
        can_tail_output=can_tail_output,
        can_resume=can_resume,
    )
    db.add(conn)
    db.flush()
    return conn
