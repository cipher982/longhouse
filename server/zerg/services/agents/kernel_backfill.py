"""Idempotent backfill helpers for the session identity kernel.

Phase 1 only creates a root thread per session and points
``sessions.primary_thread_id`` at it. Phase 3 will handle thread_id on child
tables and run/connection synthesis from existing evidence.

This module is purely additive — it never deletes or rewrites legacy rows.

See docs/specs/session-identity-kernel.md.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias


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
