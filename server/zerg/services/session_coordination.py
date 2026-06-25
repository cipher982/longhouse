"""Shared coordination helpers for the session kernel.

These helpers keep the machine-facing API routes and in-process agent tools on
the same wall/tail/message semantics without forcing a broad router rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.services.agents import AgentsStore
from zerg.services.agents.kernel_capabilities import project_capabilities_bulk
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_messages import MESSAGE_STATUS_DELIVERING
from zerg.services.session_messages import MESSAGE_STATUS_FAILED
from zerg.services.session_messages import MESSAGE_STATUS_QUEUED
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_views import WallSessionResponse


def serialize_session_message(message: SessionMessage, *, delivery_status: str | None = None) -> dict[str, Any]:
    return {
        "id": message.id,
        "from_session_id": str(message.from_session_id),
        "to_session_id": str(message.to_session_id),
        "text": message.body,
        "source_event_id": message.source_event_id,
        "delivery_status": delivery_status or message.delivery_status,
        "delivery_attempts": message.delivery_attempts,
        "last_error": message.last_error,
        "delivered_via": message.delivered_via,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "acknowledged_at": message.acknowledged_at.isoformat() if message.acknowledged_at else None,
    }


def query_wall_sessions(
    db: Session,
    *,
    repo: str | None = None,
    project: str | None = None,
    days: int = 7,
    limit: int = 50,
) -> list[WallSessionResponse]:
    """Return raw wall sessions for repo/project coordination queries."""
    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    fetch_limit = limit * 4 if repo else limit

    sessions, _total = store.list_sessions(
        project=project,
        provider=None,
        environment=None,
        include_test=False,
        device_id=None,
        since=since,
        query=None,
        limit=fetch_limit,
        offset=0,
        anchor_on_activity=True,
    )

    if repo:
        repo_lower = repo.lower()
        sessions = [
            session
            for session in sessions
            if (session.git_repo and repo_lower in session.git_repo.lower()) or (session.cwd and repo_lower in session.cwd.lower())
        ]
    sessions = sessions[:limit]

    session_ids = [session.id for session in sessions]
    last_activity = store.get_last_activity_map(session_ids)
    last_user_msg = store.get_last_timestamp_by_role_map(session_ids, "user")
    last_tool_call = store.get_last_tool_call_map(session_ids)
    runtime_state_map = load_runtime_state_map(db, session_ids)
    kernel_capabilities_map = project_capabilities_bulk(db, session_ids=session_ids)
    pending_inbound_map: dict[UUID, int] = {}
    if session_ids:
        pending_rows = (
            db.query(SessionMessage.to_session_id, func.count(SessionMessage.id))
            .filter(SessionMessage.to_session_id.in_(session_ids))
            .filter(SessionMessage.acknowledged_at.is_(None))
            .filter(SessionMessage.delivery_status != MESSAGE_STATUS_FAILED)
            .group_by(SessionMessage.to_session_id)
            .all()
        )
        pending_inbound_map = {UUID(str(session_id)): int(count) for session_id, count in pending_rows}

    now = datetime.now(timezone.utc)
    items: list[WallSessionResponse] = []
    for session in sessions:
        kernel_capabilities = kernel_capabilities_map.get(session.id)
        runtime_overlay = resolve_runtime_overlay(
            session,
            last_activity_at=last_activity.get(session.id),
            runtime_state_map=runtime_state_map,
            now=now,
        )
        has_live_presence = runtime_overlay.presence_state is not None
        presence_state = runtime_overlay.presence_state

        items.append(
            WallSessionResponse(
                session_id=str(session.id),
                device_name=getattr(session, "device_name", None)
                or (session.device_id.replace("shipper-", "") if session.device_id else None),
                device_id=session.device_id,
                cwd=session.cwd,
                git_repo=session.git_repo,
                git_branch=session.git_branch,
                project=session.project,
                provider=session.provider,
                summary_title=getattr(session, "summary_title", None),
                started_at=session.started_at,
                last_event_at=last_activity.get(session.id),
                last_user_message_at=last_user_msg.get(session.id),
                last_tool_call_at=last_tool_call.get(session.id),
                has_live_presence=has_live_presence,
                presence_state=presence_state,
                kernel_control_label=(kernel_capabilities.control_label if kernel_capabilities is not None else None),
                kernel_live_control_available=(
                    bool(kernel_capabilities.live_control_available) if kernel_capabilities is not None else False
                ),
                kernel_host_reattach_available=(
                    bool(kernel_capabilities.host_reattach_available) if kernel_capabilities is not None else False
                ),
                kernel_observe_only=(bool(kernel_capabilities.observe_only) if kernel_capabilities is not None else False),
                kernel_search_only=(bool(kernel_capabilities.search_only) if kernel_capabilities is not None else False),
                kernel_staleness_reason=(kernel_capabilities.staleness_reason if kernel_capabilities is not None else None),
                pending_inbound_messages=pending_inbound_map.get(session.id, 0),
                user_messages=session.user_messages or 0,
                assistant_messages=session.assistant_messages or 0,
                tool_calls=session.tool_calls or 0,
            )
        )

    return items


def build_peer_payloads(
    sessions: Sequence[WallSessionResponse],
    *,
    active_only: bool = True,
    exclude_session_id: UUID | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Project wall sessions into the narrower peer payload used by agents."""
    excluded_session_id = str(exclude_session_id) if exclude_session_id is not None else None
    peers: list[dict[str, Any]] = []

    for session in sessions:
        if excluded_session_id and session.session_id == excluded_session_id:
            continue
        if active_only and not session.has_live_presence:
            continue

        peers.append(
            {
                "session_id": session.session_id,
                "device_name": session.device_name,
                "provider": session.provider,
                "cwd": session.cwd,
                "git_repo": session.git_repo,
                "kernel_control_label": session.kernel_control_label,
                "kernel_live_control_available": session.kernel_live_control_available,
                "kernel_host_reattach_available": session.kernel_host_reattach_available,
                "kernel_observe_only": session.kernel_observe_only,
                "kernel_search_only": session.kernel_search_only,
                "kernel_staleness_reason": session.kernel_staleness_reason,
                "presence_state": session.presence_state,
                "pending_inbound_messages": session.pending_inbound_messages,
                "summary_title": session.summary_title,
                "git_branch": session.git_branch,
            }
        )
        if limit is not None and len(peers) >= limit:
            break

    return peers


def load_session_tail(
    db: Session,
    *,
    session_id: UUID,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return the recent tail of a session in chronological order."""
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        raise ValueError("Session not found")

    events = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.role.in_(["user", "assistant", "tool"]))
        .filter(AgentEvent.content_text.isnot(None))
        .filter(durable_transcript_event_predicate())
        .order_by(AgentEvent.timestamp.desc(), AgentEvent.id.desc())
        .limit(limit)
        .all()
    )
    events.reverse()

    return [
        {
            "id": event.id,
            "role": event.role,
            "content": (event.content_text or "")[:4000],
            "tool_name": event.tool_name,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }
        for event in events
    ]


def list_session_messages(
    db: Session,
    *,
    session_id: UUID,
    direction: str = "inbound",
    unacknowledged_only: bool = False,
    limit: int = 50,
) -> list[SessionMessage]:
    """List durable session messages for a specific session."""
    query = db.query(SessionMessage)
    if direction == "inbound":
        query = query.filter(SessionMessage.to_session_id == session_id)
    elif direction == "outbound":
        query = query.filter(SessionMessage.from_session_id == session_id)
    else:
        query = query.filter((SessionMessage.to_session_id == session_id) | (SessionMessage.from_session_id == session_id))
    if unacknowledged_only:
        query = query.filter(SessionMessage.acknowledged_at.is_(None))

    return query.order_by(SessionMessage.created_at.desc(), SessionMessage.id.desc()).limit(limit).all()


def acknowledge_session_message(
    db: Session,
    *,
    message_id: int,
    target_session_id: UUID,
) -> SessionMessage:
    """Acknowledge a delivered message for the target session."""
    message = db.query(SessionMessage).filter(SessionMessage.id == message_id).first()
    if message is None:
        raise ValueError(f"Message {message_id} not found")
    if target_session_id != message.to_session_id:
        raise PermissionError("Only the target session can acknowledge this message")
    if message.delivery_status in {MESSAGE_STATUS_QUEUED, MESSAGE_STATUS_DELIVERING}:
        raise RuntimeError("Message has not been delivered to the target session yet")
    if message.delivery_status == MESSAGE_STATUS_FAILED:
        raise RuntimeError("Failed messages cannot be acknowledged")
    if message.acknowledged_at is None:
        message.acknowledged_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(message)
    return message
