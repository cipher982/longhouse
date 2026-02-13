"""Collect unreflected sessions and existing insights for reflection analysis.

Queries sessions where reflected_at IS NULL AND summary IS NOT NULL,
groups them by project, and fetches existing insights for dedup context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.work import INSIGHT_DEDUP_WINDOW_DAYS
from zerg.models.work import Insight

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """Lightweight session data for reflection analysis."""

    id: str
    project: str | None
    provider: str
    summary: str
    summary_title: str | None
    started_at: datetime
    tool_calls: int
    user_messages: int


@dataclass
class ProjectBatch:
    """A batch of sessions for a single project, plus existing insights for dedup."""

    project: str | None
    sessions: list[SessionInfo] = field(default_factory=list)
    existing_insights: list[dict] = field(default_factory=list)


def collect_sessions(
    db: Session,
    project: str | None = None,
    window_hours: int = 24,
) -> list[ProjectBatch]:
    """Gather unreflected sessions grouped by project.

    Args:
        db: SQLAlchemy session.
        project: Filter to specific project (None = all projects).
        window_hours: How far back to look.

    Returns:
        List of ProjectBatch objects, one per project with sessions.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)

    query = db.query(AgentSession).filter(
        AgentSession.reflected_at.is_(None),
        AgentSession.summary.isnot(None),
        AgentSession.started_at >= cutoff,
    )

    if project is not None:
        query = query.filter(AgentSession.project == project)

    sessions = query.order_by(AgentSession.started_at.desc()).all()

    if not sessions:
        return []

    # Group by project
    by_project: dict[str | None, list[SessionInfo]] = {}
    for s in sessions:
        info = SessionInfo(
            id=str(s.id),
            project=s.project,
            provider=s.provider,
            summary=s.summary,
            summary_title=s.summary_title,
            started_at=s.started_at,
            tool_calls=s.tool_calls or 0,
            user_messages=s.user_messages or 0,
        )
        by_project.setdefault(s.project, []).append(info)

    # Build batches with existing insights for dedup context
    batches: list[ProjectBatch] = []
    for proj, proj_sessions in by_project.items():
        existing = _fetch_existing_insights(db, proj)
        batches.append(
            ProjectBatch(
                project=proj,
                sessions=proj_sessions,
                existing_insights=existing,
            )
        )

    return batches


def _fetch_existing_insights(db: Session, project: str | None) -> list[dict]:
    """Fetch recent insights for a project (for dedup context in LLM prompt)."""
    cutoff = datetime.now(UTC) - timedelta(days=INSIGHT_DEDUP_WINDOW_DAYS)

    query = db.query(Insight).filter(Insight.created_at >= cutoff)
    if project is not None:
        query = query.filter(Insight.project == project)
    else:
        query = query.filter(Insight.project.is_(None))

    insights = query.order_by(Insight.created_at.desc()).limit(20).all()

    return [
        {
            "id": str(i.id),
            "title": i.title,
            "insight_type": i.insight_type,
            "description": i.description,
            "confidence": i.confidence,
            "tags": i.tags,
        }
        for i in insights
    ]
