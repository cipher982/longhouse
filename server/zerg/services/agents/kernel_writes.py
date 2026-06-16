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
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid5

from sqlalchemy.exc import IntegrityError
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

    try:
        with db.begin_nested():
            db.add(
                SessionThreadAlias(
                    thread_id=thread.id,
                    provider=provider,
                    alias_kind=alias_kind,
                    alias_value=alias_value,
                )
            )
    except IntegrityError:
        # Concurrent insert won the race. The unique index made this safe;
        # we just lost the write and the existing row is good.
        pass


def resolve_primary_thread_by_provider_session_id(
    db: Session,
    *,
    provider: str,
    provider_session_id: str | None,
) -> SessionThread | None:
    """Resolve a provider root/session id to a primary thread.

    Claude root transcript ids are UUID-shaped and normally match
    ``AgentSession.id``. Other providers may use aliases. Prefer the direct
    session lookup for UUID-shaped ids, then fall back to provider aliases.
    """

    provider_session_id = str(provider_session_id or "").strip()
    if not provider_session_id:
        return None

    try:
        session_uuid = UUID(provider_session_id)
    except ValueError:
        session_uuid = None

    if session_uuid is not None:
        session = db.query(AgentSession).filter(AgentSession.id == session_uuid).one_or_none()
        if session is not None:
            return ensure_primary_thread(db, session)

    return (
        db.query(SessionThread)
        .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
        .filter(SessionThread.provider == provider)
        .filter(SessionThread.is_primary == 1)
        .filter(SessionThreadAlias.provider == provider)
        .filter(SessionThreadAlias.alias_kind == "provider_session_id")
        .filter(SessionThreadAlias.alias_value == provider_session_id)
        .order_by(SessionThread.created_at.asc(), SessionThread.id.asc())
        .first()
    )


def ensure_subagent_thread(
    db: Session,
    *,
    parent_thread: SessionThread,
    provider: str,
    source_path: str | None = None,
    child_longhouse_session_id: str | None = None,
    child_provider_session_id: str | None = None,
    subagent_id: str | None = None,
    subagent_prompt_id: str | None = None,
    subagent_tool_use_id: str | None = None,
    workflow_run_id: str | None = None,
    attribution_agent: str | None = None,
    attribution_skill: str | None = None,
    parent_provider_session_id: str | None = None,
) -> SessionThread:
    """Return a non-primary child thread for a provider subagent transcript.

    The preferred identity is the source transcript path. We derive a stable
    UUID from parent thread + child key so concurrent replays of the same child
    file converge on the same row even though aliases are intentionally not
    globally unique.
    """

    # Per-agent identity aliases: unique to THIS subagent transcript. Used both
    # to resolve an existing child thread and to label it. NOTE:
    # forked_from_provider_session_id is intentionally NOT here — it is shared by
    # every sibling subagent of a parent, so using it as an identity key would
    # collapse all of a workflow run's agents onto one thread.
    alias_pairs: list[tuple[str, str]] = []
    for kind, value in (
        ("source_path", source_path),
        ("longhouse_session_id", child_longhouse_session_id),
        ("provider_session_id", child_provider_session_id),
        ("claude_agent_id", subagent_id),
        ("claude_prompt_id", subagent_prompt_id),
        ("claude_tool_use_id", subagent_tool_use_id),
    ):
        normalized = str(value or "").strip()
        if normalized:
            alias_pairs.append((kind, normalized))

    # Shared / descriptive aliases: recorded on the thread but NOT used for
    # identity resolution (many subagents share the same parent / run id /
    # attribution; using these as identity keys would collapse distinct agents).
    label_pairs: list[tuple[str, str]] = []
    for kind, value in (
        ("forked_from_provider_session_id", parent_provider_session_id),
        ("workflow_run_id", workflow_run_id),
        ("workflow_attribution_agent", attribution_agent),
        ("workflow_attribution_skill", attribution_skill),
    ):
        normalized = str(value or "").strip()
        if normalized:
            label_pairs.append((kind, normalized))

    for alias_kind, alias_value in alias_pairs:
        existing = (
            db.query(SessionThread)
            .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
            .filter(SessionThread.session_id == parent_thread.session_id)
            .filter(SessionThread.parent_thread_id == parent_thread.id)
            .filter(SessionThread.branch_kind == "subagent")
            .filter(SessionThreadAlias.provider == provider)
            .filter(SessionThreadAlias.alias_kind == alias_kind)
            .filter(SessionThreadAlias.alias_value == alias_value)
            .order_by(SessionThread.created_at.asc(), SessionThread.id.asc())
            .first()
        )
        if existing is not None:
            thread = existing
            break
    else:
        identity_kind, identity_value = alias_pairs[0] if alias_pairs else ("generated", str(uuid5(NAMESPACE_URL, str(parent_thread.id))))
        thread_id = uuid5(
            NAMESPACE_URL,
            f"longhouse:subagent-thread:{parent_thread.id}:{provider}:{identity_kind}:{identity_value}",
        )
        thread = SessionThread(
            id=thread_id,
            session_id=parent_thread.session_id,
            provider=provider,
            parent_thread_id=parent_thread.id,
            branch_kind="subagent",
            is_primary=0,
        )
        try:
            with db.begin_nested():
                db.add(thread)
                db.flush()
        except IntegrityError:
            thread = db.query(SessionThread).filter(SessionThread.id == thread_id).one()

    for alias_kind, alias_value in alias_pairs + label_pairs:
        record_thread_alias(
            db,
            thread=thread,
            provider=provider,
            alias_kind=alias_kind,
            alias_value=alias_value,
        )
    return thread


def resolve_thread_id_for_session(db: Session, session_id) -> Optional[str]:
    """Cheap lookup: thread.id for the session's primary thread, or None.

    Returns None when no session row exists yet OR no primary thread has
    been materialized. Prefer ``ensure_thread_id_for_session`` for ingest
    write paths so child rows never carry NULL thread_id.
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


def ensure_thread_id_for_session(db: Session, session_id) -> Optional[str]:
    """Ensure-style: return the primary thread id, materializing it if absent.

    Looks up the AgentSession row and calls ``ensure_primary_thread``. If
    the session row itself does not exist (caller passed a stale id),
    returns None — caller decides whether that is a hard error.
    """

    if session_id is None:
        return None
    existing = resolve_thread_id_for_session(db, session_id)
    if existing is not None:
        return existing
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None
    thread = ensure_primary_thread(db, session)
    return thread.id


def record_launch_attempt(
    db: Session,
    *,
    session: AgentSession,
    thread: SessionThread | None,
    provider: str,
    host_id: str | None,
    owner_id: int | None = None,
    execution_lifetime: str = "live_control",
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
        owner_id=owner_id,
        execution_lifetime=execution_lifetime,
        client_request_id=client_request_id,
        command_id=command_id,
        state=state,
        expires_at=expires_at,
    )
    try:
        with db.begin_nested():
            db.add(attempt)
            db.flush()
    except IntegrityError:
        # Concurrent caller won; re-read the row that landed first.
        if not client_request_id:
            raise
        existing = (
            db.query(SessionLaunchAttempt)
            .filter(
                SessionLaunchAttempt.session_id == session.id,
                SessionLaunchAttempt.client_request_id == client_request_id,
            )
            .one_or_none()
        )
        if existing is None:
            raise
        return existing
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
    device_id: str | None = None,
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
        try:
            with db.begin_nested():
                return record_connection(
                    db,
                    run=run,
                    control_plane=control_plane,
                    acquisition_kind=acquisition_kind,
                    state=state,
                    external_name=external_name,
                    device_id=device_id,
                    can_send_input=can_send_input or 0,
                    can_interrupt=can_interrupt or 0,
                    can_terminate=can_terminate or 0,
                    can_tail_output=can_tail_output or 0,
                    can_resume=can_resume or 0,
                )
        except IntegrityError:
            # Concurrent caller landed first; fall through to the update path.
            existing = (
                db.query(SessionConnection)
                .filter(
                    SessionConnection.run_id == run.id,
                    SessionConnection.control_plane == control_plane,
                )
                .order_by(SessionConnection.id.desc())
                .first()
            )
            if existing is None:
                raise

    if existing.state != state:
        existing.state = state
        if state in {"detached", "released", "ended"}:
            existing.released_at = now
        elif state == "attached":
            existing.released_at = None
    if external_name is not None and existing.external_name != external_name:
        existing.external_name = external_name
    if device_id is not None and existing.device_id != device_id:
        existing.device_id = device_id
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
    device_id: str | None = None,
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
        device_id=device_id,
        can_send_input=can_send_input,
        can_interrupt=can_interrupt,
        can_terminate=can_terminate,
        can_tail_output=can_tail_output,
        can_resume=can_resume,
    )
    db.add(conn)
    db.flush()
    return conn
