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
from zerg.services.agents.kernel_capabilities import project_capabilities_bulk
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_turns import load_pending_response_turn_map
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import build_session_response
from zerg.services.session_views import normalize_utc_datetime
from zerg.services.unmanaged_bindings import load_binding_overlay

# Mirrors zerg.services.session_summaries — fewer than this many user+assistant
# messages means the summarizer waits instead of attempting; treat as
# "unavailable" rather than "pending".
SUMMARY_MIN_MEANINGFUL_MESSAGES = 2


def derive_summary_status(
    *,
    summary: str | None,
    user_messages: int | None,
    assistant_messages: int | None,
    transcript_revision: int | None,
    summary_revision: int | None,
) -> str:
    """Derive summary lifecycle from session revision lag.

    Tiebreaker: existing summary text wins. A current empty summary is a real
    state for low-content or no-new-event sessions after the summarizer advances
    summary_revision.
    """
    if summary and summary.strip():
        return "ready"
    meaningful_messages = (user_messages or 0) + (assistant_messages or 0)
    transcript_rev = transcript_revision or 0
    summary_rev = summary_revision or 0
    if transcript_rev <= 0:
        return "unavailable"
    if meaningful_messages < SUMMARY_MIN_MEANINGFUL_MESSAGES:
        return "unavailable"
    if summary_rev < transcript_rev:
        return "pending"
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
    control_state_map = load_managed_control_state_map(db, session_ids)
    transcript_preview_map = load_active_provisional_preview_map(db, session_ids)
    pending_response_turn_map = load_pending_response_turn_map(db, session_ids)
    first_user_map = store.get_first_message_map(session_ids, role="user", max_len=80)
    thread_cache: dict[str, tuple[str, int]] = store.batch_thread_meta(sessions)
    binding_overlay_map = load_binding_overlay(db, session_ids, now=now)
    kernel_capabilities_map = project_capabilities_bulk(db, session_ids=session_ids)
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
            assistant_messages=session.assistant_messages,
            transcript_revision=session.transcript_revision,
            summary_revision=session.summary_revision,
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
                control_overlay=control_state_map.get(session.id),
                transcript_preview=transcript_preview_map.get(str(session.id)),
                owner_id=owner_id,
                summary_status=summary_status,
                kernel_capabilities=kernel_capabilities_map.get(session.id),
                has_pending_response_turn=bool(pending_response_turn_map.get(session.id)),
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
        .filter(or_(AgentSession.provider != "canary", AgentSession.provider.is_(None)))
        .limit(1)
        .first()
        is not None
    )
