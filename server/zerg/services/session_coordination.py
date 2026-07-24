"""Shared coordination helpers for the session kernel.

These helpers keep the machine-facing API routes and agent adapters on the same
session discovery and tail semantics.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard
from zerg.services.agents import AgentsStore
from zerg.services.agents.kernel_capabilities import project_capabilities_bulk
from zerg.services.agents.kernel_capabilities import project_capabilities_from_rows
from zerg.services.catalog_facts import decode_catalog_datetime
from zerg.services.catalog_facts import hydrate_catalog_row
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_runtime import build_runtime_view
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_views import WallSessionResponse


def query_wall_sessions(
    db: Session,
    *,
    repo: str | None = None,
    project: str | None = None,
    days: int = 7,
    limit: int = 50,
    include_automation: bool = False,
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
        include_automation=include_automation,
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
                user_messages=session.user_messages or 0,
                assistant_messages=session.assistant_messages or 0,
                tool_calls=session.tool_calls or 0,
            )
        )

    return items


def project_storage_v2_wall(
    snapshot: dict[str, Any],
    *,
    repo: str | None = None,
    limit: int = 50,
) -> list[WallSessionResponse]:
    """Project catalogd timeline facts into the wall contract without a DB.

    The timeline snapshot already owns filtering, ordering, and pagination.
    Event-role timestamps are intentionally left unset because the bounded
    catalog does not persist them; ``last_event_at`` remains the canonical
    activity signal available from storage-v2.
    """

    observed_at = decode_catalog_datetime(snapshot.get("observed_at"))
    if not isinstance(observed_at, datetime):
        raise ValueError("catalog timeline snapshot is missing observed_at")
    if limit <= 0:
        return []
    repo_lower = repo.lower() if repo else None

    items: list[WallSessionResponse] = []
    for row in snapshot.get("rows") or []:
        facts = row.get("facts") if isinstance(row, dict) else None
        if not isinstance(facts, dict):
            raise ValueError("catalog timeline row is missing facts")
        session = hydrate_catalog_row(LiveSessionCatalog, facts.get("catalog"))
        if session is None:
            raise ValueError("catalog wall facts are missing catalog")
        if repo_lower and not (
            (session.git_repo and repo_lower in session.git_repo.lower()) or (session.cwd and repo_lower in session.cwd.lower())
        ):
            continue
        card = hydrate_catalog_row(LiveTimelineCard, facts.get("card"))
        runtime = hydrate_catalog_row(LiveRuntimeState, facts.get("runtime"))
        thread = hydrate_catalog_row(LiveSessionThread, facts.get("primary_thread"))
        run = hydrate_catalog_row(LiveSessionRun, facts.get("latest_run"))
        connections = [
            connection
            for payload in facts.get("connections") or []
            if (connection := hydrate_catalog_row(LiveSessionConnection, payload)) is not None
        ]
        capabilities = project_capabilities_from_rows(
            session_id=str(session.session_id),
            thread=thread,
            latest_run=run,
            connections=connections,
            now=observed_at,
        )
        runtime_view = build_runtime_view(state=runtime, session=session, now=observed_at) if runtime is not None else None
        last_activity_at = (card.last_activity_at if card is not None else None) or session.last_activity_at
        session_id = str(session.session_id)

        items.append(
            WallSessionResponse(
                session_id=session_id,
                device_name=session.device_name or (session.device_id.replace("shipper-", "") if session.device_id else None),
                device_id=session.device_id,
                cwd=session.cwd,
                git_repo=session.git_repo,
                git_branch=session.git_branch,
                project=session.project,
                provider=session.provider,
                summary_title=(card.summary_title if card is not None else None) or session.summary_title,
                started_at=session.started_at,
                last_event_at=last_activity_at,
                has_live_presence=runtime_view is not None and runtime_view.presence_state is not None,
                presence_state=runtime_view.presence_state if runtime_view is not None else None,
                kernel_control_label=capabilities.control_label,
                kernel_live_control_available=capabilities.live_control_available,
                kernel_host_reattach_available=capabilities.host_reattach_available,
                kernel_observe_only=capabilities.observe_only,
                kernel_search_only=capabilities.search_only,
                kernel_staleness_reason=capabilities.staleness_reason,
                user_messages=int((card.user_messages if card is not None else session.user_messages) or 0),
                assistant_messages=int((card.assistant_messages if card is not None else session.assistant_messages) or 0),
                tool_calls=int((card.tool_calls if card is not None else session.tool_calls) or 0),
            )
        )
        if len(items) >= limit:
            break

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
