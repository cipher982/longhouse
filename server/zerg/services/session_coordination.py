"""Shared coordination helpers for the session kernel.

These helpers keep the machine-facing API routes and agent adapters on the same
session discovery and tail semantics.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Sequence
from datetime import datetime
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
from zerg.services.agents.kernel_capabilities import project_capabilities_from_rows
from zerg.services.catalog_facts import decode_catalog_datetime
from zerg.services.catalog_facts import hydrate_catalog_row
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_runtime import build_runtime_view
from zerg.services.session_views import WallSessionResponse


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
    roles: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the recent tail of a session in chronological order.

    ``roles`` narrows which event roles count toward ``limit``, so a caller can
    ask for real turns instead of tool output.

    Content is returned whole. The per-event budget belongs to the caller that
    knows what the requester asked for, so the router owns the single
    truncation site.
    """
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        raise ValueError("Session not found")

    selected_roles = sorted(roles) if roles else ["user", "assistant", "tool"]
    events = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(AgentEvent.role.in_(selected_roles))
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
            "content": event.content_text or "",
            "tool_name": event.tool_name,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }
        for event in events
    ]
