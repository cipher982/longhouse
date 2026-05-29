"""Work tracking models — insights and runner wakeups.

These models support agent infrastructure: tracking reusable learnings across
sessions and recording proactive runner wakeups.
"""

from uuid import uuid4

from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import true
from sqlalchemy.sql import func

from zerg.models.agents import AgentsBase
from zerg.models.types import GUID

# Shared constant: dedup window for insights (used by router)
INSIGHT_DEDUP_WINDOW_DAYS = 7
INSIGHT_ORIGIN_MANUAL = "manual"
INSIGHT_ORIGIN_SYSTEM = "system"


class Insight(AgentsBase):
    """A learning, pattern, failure, or improvement insight from agent sessions."""

    __tablename__ = "insights"

    id = Column(GUID(), primary_key=True, default=uuid4)
    insight_type = Column(String(20), nullable=False)  # pattern, failure, improvement, learning
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    project = Column(String(255), nullable=True, index=True)
    origin = Column(String(20), nullable=True, default=INSIGHT_ORIGIN_MANUAL, index=True)
    severity = Column(String(20), default="info")  # info, warning, critical
    confidence = Column(Float, nullable=True)  # 0.0-1.0
    tags = Column(JSON, nullable=True)
    observations = Column(JSON, nullable=True)  # Append-only list of sightings
    session_id = Column(GUID(), nullable=True)  # Source session (optional)
    archived_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class RunnerWakeup(AgentsBase):
    """Durable record of a proactive runner wakeup opportunity."""

    __tablename__ = "runner_wakeups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, nullable=True, index=True)
    source = Column(String(64), nullable=False, index=True)
    trigger_type = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, index=True)  # suppressed, enqueued, ignored, acted, failed
    reason = Column(String(100), nullable=True)
    session_id = Column(String(255), nullable=True, index=True)
    conversation_id = Column(String(255), nullable=True)
    wakeup_key = Column(String(255), nullable=True, index=True)
    run_id = Column(Integer, nullable=True, index=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    __table_args__ = (
        Index("ix_runner_wakeups_owner_created", "owner_id", "created_at"),
        Index("ix_runner_wakeups_trigger_status", "trigger_type", "status"),
    )


def insight_visibility_clause(model=Insight, *, include_system: bool = False, include_archived: bool = False):
    """Shared visibility filter for continuity-facing insight reads."""
    clauses = []
    if not include_system:
        clauses.append(or_(model.origin.is_(None), model.origin != INSIGHT_ORIGIN_SYSTEM))
    if not include_archived:
        clauses.append(model.archived_at.is_(None))
    if not clauses:
        return true()
    return and_(*clauses)


def user_visible_insight_clause(model=Insight):
    """Default continuity view: hide system and archived insights."""
    return insight_visibility_clause(model)
