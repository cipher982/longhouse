"""Write helpers for the Longhouse session graph.

This module owns graph mechanics only: primary threads, child threads, aliases,
lineage edges, and provider-session lookup. It intentionally does not know
about provider runs, connections, launch attempts, or control state.
"""

from __future__ import annotations

from typing import Optional
from uuid import NAMESPACE_URL
from uuid import uuid5

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionEdge
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias


def ensure_primary_thread(db: Session, session: AgentSession) -> SessionThread:
    """Return the primary thread for ``session``, creating it if needed."""

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
    """Record provider/source evidence for ``thread`` if it is not present."""

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
        pass


def record_session_edge(
    db: Session,
    *,
    provider: str,
    edge_kind: str,
    visibility: str,
    evidence_kind: str | None = None,
    source_thread: SessionThread | None = None,
    target_thread: SessionThread | None = None,
    provider_edge_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Record semantic session-graph relationship evidence if not present."""

    metadata = {key: value for key, value in dict(metadata or {}).items() if value is not None}
    provider_edge_id = str(provider_edge_id or "").strip() or None
    query = db.query(SessionEdge).filter(SessionEdge.provider == provider).filter(SessionEdge.edge_kind == edge_kind)
    if provider_edge_id:
        query = query.filter(SessionEdge.provider_edge_id == provider_edge_id)
    else:
        query = query.filter(SessionEdge.source_thread_id == (source_thread.id if source_thread else None))
        query = query.filter(SessionEdge.target_thread_id == (target_thread.id if target_thread else None))
    if query.first() is not None:
        return

    try:
        with db.begin_nested():
            db.add(
                SessionEdge(
                    provider=provider,
                    edge_kind=edge_kind,
                    visibility=visibility,
                    evidence_kind=evidence_kind,
                    source_session_id=source_thread.session_id if source_thread else None,
                    source_thread_id=source_thread.id if source_thread else None,
                    target_session_id=target_thread.session_id if target_thread else None,
                    target_thread_id=target_thread.id if target_thread else None,
                    provider_edge_id=provider_edge_id,
                    metadata_json=metadata or None,
                )
            )
    except IntegrityError:
        pass


def resolve_primary_thread_by_provider_session_id(
    db: Session,
    *,
    provider: str,
    provider_session_id: str | None,
) -> SessionThread | None:
    """Resolve provider root/session evidence to a primary thread."""

    return resolve_thread_by_provider_session_id(
        db,
        provider=provider,
        provider_session_id=provider_session_id,
        primary_only=True,
    )


def resolve_thread_by_provider_session_id(
    db: Session,
    *,
    provider: str,
    provider_session_id: str | None,
    primary_only: bool = False,
) -> SessionThread | None:
    """Resolve provider session evidence to any materialized thread.

    OpenCode subagents can delegate to other subagents and resume prior task
    sessions, so child-session parentage cannot be limited to primary threads.
    """

    provider_session_id = str(provider_session_id or "").strip()
    if not provider_session_id:
        return None

    query = (
        db.query(SessionThread)
        .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
        .filter(SessionThread.provider == provider)
        .filter(SessionThreadAlias.provider == provider)
        .filter(SessionThreadAlias.alias_kind == "provider_session_id")
        .filter(SessionThreadAlias.alias_value == provider_session_id)
    )
    if primary_only:
        query = query.filter(SessionThread.is_primary == 1)
    return query.order_by(SessionThread.is_primary.desc(), SessionThread.created_at.asc(), SessionThread.id.asc()).first()


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
    """Return the child thread for a provider subagent transcript."""

    provider_name = str(provider or "").strip().lower()
    alias_pairs: list[tuple[str, str]] = []
    common_aliases = (
        ("source_path", source_path),
        ("longhouse_session_id", child_longhouse_session_id),
        ("provider_session_id", child_provider_session_id),
        ("subagent_id", subagent_id),
        ("subagent_prompt_id", subagent_prompt_id),
        ("subagent_tool_use_id", subagent_tool_use_id),
    )
    legacy_provider_aliases: tuple[tuple[str, str | None], ...] = ()
    if provider_name == "claude":
        legacy_provider_aliases = (
            ("claude_agent_id", subagent_id),
            ("claude_prompt_id", subagent_prompt_id),
            ("claude_tool_use_id", subagent_tool_use_id),
        )
    for kind, value in (*common_aliases, *legacy_provider_aliases):
        normalized = str(value or "").strip()
        if normalized:
            alias_pairs.append((kind, normalized))

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
    """Return the session primary thread id, if materialized."""

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
    """Return the primary thread id, materializing it when possible."""

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
