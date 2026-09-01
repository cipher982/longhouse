"""Workspace suggestions for the session-launch picker.

Server-owned, frecency-ranked, git-labeled list of recent working directories
for one machine. Both the iOS launch sheet and the web launch modal consume
this instead of re-deriving suggestions client-side from the timeline.

Scoped strictly by ``device_id`` (no ``environment`` fallback): the picker
lists ``device_id`` values, so suggestions must match the same axis or a
renamed machine's ghost history leaks in. See
``services.machines_directory`` for the device list this pairs with.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.models.device_token import DeviceToken
from zerg.services.workspace_suggestion_projection import WORKSPACE_CANDIDATE_MAX_PAGES
from zerg.services.workspace_suggestion_projection import WORKSPACE_CANDIDATE_PAGE_SIZE
from zerg.services.workspace_suggestion_projection import WorkspaceSessionFacts
from zerg.services.workspace_suggestion_projection import WorkspaceSuggestionEntry
from zerg.services.workspace_suggestion_projection import rank_human_workspace_candidates


def build_workspace_suggestions(
    db: Session,
    *,
    owner_id: int,
    device_id: str,
    limit: int = 12,
    days_back: int = 45,
    session_model=AgentSession,
) -> list[WorkspaceSuggestionEntry]:
    """Ranked recent workspaces for ``device_id`` owned by ``owner_id``.

    Returns ``[]`` for an unknown/unenrolled device so the picker degrades to
    manual path entry instead of erroring.
    """
    session_model = session_model or AgentSession
    enrolled = (
        db.query(DeviceToken.id)
        .filter(
            DeviceToken.owner_id == owner_id,
            DeviceToken.device_id == device_id,
            DeviceToken.revoked_at.is_(None),
        )
        .first()
    )
    if enrolled is None:
        return []

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days_back)
    stmt = (
        select(
            session_model.device_id,
            session_model.provider,
            session_model.environment,
            session_model.project,
            session_model.cwd,
            session_model.git_repo,
            session_model.git_branch,
            session_model.last_activity_at,
            session_model.started_at,
            session_model.origin_kind,
            session_model.hidden_from_default_timeline,
            session_model.user_hidden_from_timeline,
            session_model.launch_actor,
            SessionThread.branch_kind,
        )
        .outerjoin(
            SessionThread,
            and_(SessionThread.session_id == session_model.id, SessionThread.is_primary == 1),
        )
        .where(
            session_model.device_id == device_id,
            func.coalesce(session_model.last_activity_at, session_model.started_at) >= since,
        )
        .order_by(
            func.coalesce(session_model.last_activity_at, session_model.started_at).desc(),
            session_model.id.desc(),
        )
    )
    candidates: list[WorkspaceSessionFacts] = []
    for page in range(WORKSPACE_CANDIDATE_MAX_PAGES):
        rows = db.execute(stmt.offset(page * WORKSPACE_CANDIDATE_PAGE_SIZE).limit(WORKSPACE_CANDIDATE_PAGE_SIZE + 1)).all()
        has_more = len(rows) > WORKSPACE_CANDIDATE_PAGE_SIZE
        page_facts = [
            WorkspaceSessionFacts(
                device_id=row.device_id,
                provider=row.provider,
                environment=row.environment,
                project=row.project,
                cwd=row.cwd,
                git_repo=row.git_repo,
                git_branch=row.git_branch,
                last_activity_at=row.last_activity_at,
                started_at=row.started_at,
                origin_kind=row.origin_kind,
                hidden_from_default_timeline=bool(row.hidden_from_default_timeline),
                user_hidden_from_timeline=bool(row.user_hidden_from_timeline),
                launch_actor=row.launch_actor,
                is_sidechain=row.branch_kind == "subagent",
            )
            for row in rows[:WORKSPACE_CANDIDATE_PAGE_SIZE]
        ]
        candidates.extend(page_facts)
        if not has_more:
            break
    return rank_human_workspace_candidates(
        candidates,
        device_id=device_id,
        now=now,
        days_back=days_back,
        limit=limit,
    )
