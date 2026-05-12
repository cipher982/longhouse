"""Shared batch projection from AgentSession rows to SessionResponse models."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Mapping
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import build_session_response
from zerg.services.session_views import normalize_utc_datetime
from zerg.services.unmanaged_bindings import load_binding_overlay


def build_session_response_list(
    *,
    db: Session,
    store: AgentsStore,
    sessions: list[AgentSession],
    match_map: Mapping[Any, Mapping[str, Any]] | None = None,
    semantic_snippet_map: Mapping[str, str] | None = None,
    sem_score_map: Mapping[Any, float] | None = None,
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
    match_map = match_map or {}
    semantic_snippet_map = semantic_snippet_map or {}
    sem_score_map = sem_score_map or {}

    responses: list[SessionResponse] = []
    for session in sessions:
        last_activity_at = activity_map.get(session.id) or session.ended_at or session.started_at
        match = match_map.get(session.id) or {}
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
            )
        )

    return responses


def build_session_response_map(
    *,
    db: Session,
    session_ids: list[str],
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
