"""Shared batch projection from AgentSession rows to SessionResponse models."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Mapping
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services.agents_store import AgentsStore
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import build_session_response
from zerg.services.session_views import normalize_utc_datetime
from zerg.services.unmanaged_bindings import load_binding_overlay

# Mirrors zerg.services.ingest_task_queue.RESURRECT_MAX_CYCLES — when a summary
# task has been resurrected this many times and is still failed, the house
# cleaner stops retrying and the task is genuinely terminal.
SUMMARY_TERMINAL_RESURRECTION_COUNT = 5

# Mirrors zerg.services.session_summaries — fewer than this many user+assistant
# messages means the summarizer waits instead of attempting; treat as
# "unavailable" rather than "pending".
SUMMARY_MIN_MEANINGFUL_MESSAGES = 2


def load_summary_status_map(db: Session, session_ids: list[str]) -> dict[str, str | None]:
    """Single-query lookup of the latest 'summary' SessionTask per session.

    Returns a map of session_id -> 'pending' | 'failed' | None. ('ready' and
    'unavailable' are derived in the caller using session.summary and
    user_messages — they don't require a task row.)

    Strategy: ONE query — for each session, fetch the latest summary task by
    updated_at. SQLite supports correlated MAX subqueries; using a small IN
    list keeps this O(rows in summary tasks for these sessions) and avoids N+1.
    """
    if not session_ids:
        return {}

    # Latest updated_at per (session_id) for summary tasks
    latest_subq = (
        db.query(
            SessionTask.session_id.label("sid"),
            func.max(SessionTask.updated_at).label("max_updated_at"),
        )
        .filter(
            SessionTask.session_id.in_(session_ids),
            SessionTask.task_type == "summary",
        )
        .group_by(SessionTask.session_id)
        .subquery()
    )

    rows = (
        db.query(
            SessionTask.session_id,
            SessionTask.status,
            SessionTask.resurrection_count,
        )
        .join(
            latest_subq,
            (SessionTask.session_id == latest_subq.c.sid)
            & (SessionTask.updated_at == latest_subq.c.max_updated_at)
            & (SessionTask.task_type == "summary"),
        )
        .all()
    )

    out: dict[str, str | None] = {}
    for sid, status, resurrection_count in rows:
        if status in ("pending", "running"):
            out[str(sid)] = "pending"
        elif status == "failed" and (resurrection_count or 0) >= SUMMARY_TERMINAL_RESURRECTION_COUNT:
            out[str(sid)] = "failed"
        else:
            # status == "done" with no current summary, or non-terminal failed
            # (will resurrect) — leave as None so caller falls through to
            # "unavailable".
            out[str(sid)] = None
    return out


def derive_summary_status(
    *,
    summary: str | None,
    user_messages: int | None,
    task_state: str | None,
) -> str:
    """Tiebreaker: ready > pending > failed > unavailable."""
    if summary and summary.strip():
        return "ready"
    if task_state == "pending":
        return "pending"
    if task_state == "failed":
        return "failed"
    if (user_messages or 0) < SUMMARY_MIN_MEANINGFUL_MESSAGES:
        return "unavailable"
    # No task row but enough content — treat as unavailable; the worker will
    # eventually create a task on next ingest.
    return "unavailable"


def build_session_response_list(
    *,
    db: Session,
    store: AgentsStore,
    sessions: list[AgentSession],
    match_map: Mapping[Any, Mapping[str, Any]] | None = None,
    semantic_snippet_map: Mapping[str, str] | None = None,
    sem_score_map: Mapping[Any, float] | None = None,
    owner_id: int | None = None,
) -> list[SessionResponse]:
    if not sessions:
        return []

    session_ids = [session.id for session in sessions]
    activity_map = store.get_last_activity_map(session_ids)
    now = datetime.now(timezone.utc)
    runtime_state_map = load_runtime_state_map(db, session_ids)
    transcript_preview_map = load_active_provisional_preview_map(db, session_ids)
    first_user_map = store.get_first_message_map(session_ids, role="user", max_len=80)
    thread_cache: dict[str, tuple[str, int]] = store.batch_thread_meta(sessions)
    binding_overlay_map = load_binding_overlay(db, session_ids, now=now)
    summary_task_state_map = load_summary_status_map(db, [str(sid) for sid in session_ids])
    match_map = match_map or {}
    semantic_snippet_map = semantic_snippet_map or {}
    sem_score_map = sem_score_map or {}

    responses: list[SessionResponse] = []
    for session in sessions:
        last_activity_at = activity_map.get(session.id) or session.ended_at or session.started_at
        match = match_map.get(session.id) or {}
        summary_status = derive_summary_status(
            summary=session.summary,
            user_messages=session.user_messages,
            task_state=summary_task_state_map.get(str(session.id)),
        )
        responses.append(
            build_session_response(
                store,
                session,
                thread_cache=thread_cache,
                last_activity_at=normalize_utc_datetime(last_activity_at),
                runtime_overlay=resolve_runtime_overlay(
                    session,
                    last_activity_at=last_activity_at,
                    runtime_state_map=runtime_state_map,
                    now=now,
                ),
                first_user_message=first_user_map.get(session.id),
                match_event_id=match.get("event_id"),
                match_snippet=match.get("snippet") or semantic_snippet_map.get(str(session.id)),
                match_role=match.get("role"),
                match_score=sem_score_map.get(session.id),
                binding_overlay=binding_overlay_map.get(session.id),
                transcript_preview=transcript_preview_map.get(str(session.id)),
                owner_id=owner_id,
                summary_status=summary_status,
            )
        )

    return responses


def build_session_response_map(
    *,
    db: Session,
    session_ids: list[str],
    owner_id: int | None = None,
) -> dict[str, SessionResponse]:
    if not session_ids:
        return {}

    store = AgentsStore(db)
    uuid_ids = [UUID(session_id) for session_id in session_ids]
    sessions = db.query(AgentSession).filter(AgentSession.id.in_(uuid_ids)).all()
    responses = build_session_response_list(
        db=db,
        store=store,
        sessions=sessions,
        owner_id=owner_id,
    )
    return {response.id: response for response in responses}


def has_real_sessions(db: Session, *, default_when_empty: bool) -> bool:
    if default_when_empty:
        return True

    return (
        db.query(AgentSession.id)
        .filter(
            or_(
                AgentSession.device_id != "demo-mac",
                AgentSession.device_id.is_(None),
            )
        )
        .limit(1)
        .first()
        is not None
    )
