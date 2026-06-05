"""Shared resolver for native session-continuation targets.

Both the capability view (`session_views._native_continue_target`, which lights
the web Continue button) and the execution path
(`remote_session_launch._resolve_continue_target`, which dispatches the resume)
must agree on whether a session is continuable and what provider id to resume.
Keeping the logic in one place prevents view/execute drift — the bug class where
the button appears but the launch rejects, or vice versa.

Key identity fact: the provider resume id comes from the
``provider_session_id`` thread alias (the real provider identity ingested from
transcript metadata), NOT ``AgentSession.provider_session_id`` (a compatibility
shim that just returns ``str(session.id)``).

Adoption modes:
  - ``managed_resume``: the session was already managed by Longhouse (proven by a
    control-acquisition connection). Resuming re-launches it under management.
  - ``adopt_unmanaged``: an imported/raw transcript the user is EXPLICITLY
    bringing under management by clicking Continue. Honest, user-initiated — not
    a silent engine takeover.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.services.agents.kernel_capabilities import thread_ever_had_managed_control
from zerg.services.managed_provider_contracts import continue_supported_providers

AdoptionMode = Literal["managed_resume", "adopt_unmanaged"]


@dataclass(frozen=True)
class NativeContinueTargetResolution:
    """Resolved continuation target for a session.

    ``provider_resume_id`` is the id passed to the provider's resume flag
    (e.g. ``claude --resume <id>``). ``source_path`` is the local transcript
    path when known (required for codex; informational for claude).
    """

    thread: SessionThread
    provider_resume_id: str
    source_path: str | None
    adoption_mode: AdoptionMode


def _primary_thread(db: Session, session: AgentSession) -> SessionThread | None:
    return (
        db.query(SessionThread)
        .filter(SessionThread.session_id == session.id, SessionThread.is_primary == 1)
        .order_by(SessionThread.created_at.asc(), SessionThread.id.asc())
        .first()
    )


def _latest_alias(db: Session, *, thread: SessionThread, provider: str, alias_kind: str) -> str:
    value = (
        db.query(SessionThreadAlias.alias_value)
        .filter(SessionThreadAlias.thread_id == thread.id)
        .filter(SessionThreadAlias.provider == provider)
        .filter(SessionThreadAlias.alias_kind == alias_kind)
        .order_by(SessionThreadAlias.last_seen_at.desc(), SessionThreadAlias.id.desc())
        .limit(1)
        .scalar()
    )
    return str(value).strip() if value else ""


def _bounded_source_path(db: Session, *, session_id) -> str:
    """Source-transcript evidence without a thread_id OR-scan on the hot path.

    Hosted tenant DBs can have millions of source lines; a
    ``thread_id = ? OR thread_id IS NULL`` predicate has chosen a full-table OR
    plan and blocked timeline cards. We only need evidence a local transcript
    exists, so stay bounded to the session index.
    """
    value = (
        db.query(AgentSourceLine.source_path)
        .filter(AgentSourceLine.session_id == session_id)
        .order_by(AgentSourceLine.id.desc())
        .limit(1)
        .scalar()
    )
    return str(value).strip() if value else ""


def _thread_scoped_source_path(db: Session, *, session_id, thread_id) -> str:
    """The latest source path scoped to a specific thread.

    Codex resumes by transcript PATH, so it must use the path for the resumed
    thread when thread-scoped source evidence exists. Avoid a
    ``thread_id = ? OR thread_id IS NULL`` predicate here: on hosted tenant
    DBs with millions of source lines, SQLite has picked a full-table OR plan
    and blocked timeline/session-list cards. If there is no thread-specific
    row, fall back to bounded session-level evidence rather than scanning all
    NULL-thread rows.
    """
    row = (
        db.query(AgentSourceLine.source_path)
        .filter(AgentSourceLine.session_id == session_id)
        .filter(AgentSourceLine.thread_id == thread_id)
        .order_by(AgentSourceLine.id.desc())
        .limit(1)
        .scalar()
    )
    if row:
        return str(row).strip()
    return _bounded_source_path(db, session_id=session_id)


def resolve_native_continue_target(db: Session, session: AgentSession) -> NativeContinueTargetResolution | None:
    """Resolve whether ``session`` is natively continuable, and how.

    Returns ``None`` when the session cannot be continued. Pure read; never
    mutates. Both the view and execute paths call this.
    """

    provider = (session.provider or "").strip().lower()
    if provider not in continue_supported_providers():
        return None
    thread = _primary_thread(db, session)
    if thread is None:
        return None

    provider_alias = _latest_alias(db, thread=thread, provider=session.provider, alias_kind="provider_session_id")

    if provider == "claude":
        # Managed claude: control-acquisition connection proves Longhouse owned
        # the session. Resume id is the provider alias when present (real
        # provider identity), else the longhouse id (legacy managed launch pinned
        # `claude --session-id <session.id>` without recording an alias).
        if thread_ever_had_managed_control(db, thread_id=thread.id):
            alias_source = _latest_alias(db, thread=thread, provider=session.provider, alias_kind="source_path")
            resume_id = provider_alias or str(session.id)
            return NativeContinueTargetResolution(
                thread=thread,
                provider_resume_id=resume_id,
                source_path=alias_source or None,
                adoption_mode="managed_resume",
            )
        # Unmanaged/raw claude transcript: the user can EXPLICITLY adopt it by
        # clicking Continue, which launches a fresh managed process resuming the
        # provider's own session id. Gate on:
        #   - real provider identity (alias) + transcript evidence, AND
        #   - the session is CLOSED. A still-live raw `claude` process must not be
        #     duplicated by a fresh managed resume (two owners, one transcript) —
        #     that is the exact contention the "one execution owner" doctrine
        #     guards against.
        if session.ended_at is None:
            return None
        source_path = _bounded_source_path(db, session_id=session.id)
        if provider_alias and source_path:
            return NativeContinueTargetResolution(
                thread=thread,
                provider_resume_id=provider_alias,
                source_path=source_path,
                adoption_mode="adopt_unmanaged",
            )
        return None

    # codex: requires a real provider thread id (distinct from the longhouse id)
    # plus a local transcript path to resume against. Adoption of unmanaged codex
    # is out of scope here (codex resume identity differs); keep existing gate.
    if not provider_alias or provider_alias == str(session.id):
        return None
    source_path = _latest_alias(db, thread=thread, provider=session.provider, alias_kind="source_path")
    if not source_path:
        source_path = _thread_scoped_source_path(db, session_id=session.id, thread_id=thread.id)
    if not source_path:
        return None
    return NativeContinueTargetResolution(
        thread=thread,
        provider_resume_id=provider_alias,
        source_path=source_path,
        adoption_mode="managed_resume",
    )
