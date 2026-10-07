"""Shared coordination helpers for the session kernel.

These helpers keep the machine-facing API routes and agent adapters on the same
session discovery and tail semantics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard
from zerg.services.agents.kernel_capabilities import project_capabilities_from_rows
from zerg.services.catalog_facts import decode_catalog_datetime
from zerg.services.catalog_facts import hydrate_catalog_row
from zerg.services.live_catalog_timeline import project_catalog_timeline_row
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
    commit_seq = int(snapshot.get("commit_seq") or 0)

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
        # Presence is exactly the timeline card's served value. The projector
        # already demotes expired evidence (presence_state is then None), so
        # the wall applies no rule of its own.
        presence_state = project_catalog_timeline_row(row, observed_at=observed_at, commit_seq=commit_seq, surface="wall").presence_state
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
                has_live_presence=presence_state is not None,
                presence_state=presence_state,
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
