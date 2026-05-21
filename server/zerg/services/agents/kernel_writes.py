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

from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
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
