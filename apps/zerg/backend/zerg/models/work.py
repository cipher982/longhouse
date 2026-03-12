"""Work tracking models — insights, proposals, and wakeups.

These models support agent infrastructure: tracking learnings across sessions,
surfacing actionable proposals for human review, and recording proactive Oikos
wakeups.
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
from sqlalchemy.sql import func

from zerg.models.agents import AgentsBase
from zerg.models.types import GUID

# Shared constant: dedup window for insights (used by router, reflection service)
INSIGHT_DEDUP_WINDOW_DAYS = 7


class Insight(AgentsBase):
    """A learning, pattern, failure, or improvement insight from agent sessions."""

    __tablename__ = "insights"

    id = Column(GUID(), primary_key=True, default=uuid4)
    insight_type = Column(String(20), nullable=False)  # pattern, failure, improvement, learning
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    project = Column(String(255), nullable=True, index=True)
    severity = Column(String(20), default="info")  # info, warning, critical
    confidence = Column(Float, nullable=True)  # 0.0-1.0
    tags = Column(JSON, nullable=True)
    observations = Column(JSON, nullable=True)  # Append-only list of sightings
    session_id = Column(GUID(), nullable=True)  # Source session (optional)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ActionProposal(AgentsBase):
    """A proposed action derived from a high-confidence insight.

    Created during reflection when the judge attaches an action_blurb to an
    insight.  Users review proposals in the Longhouse UI and approve or decline
    them.  Approved proposals become tasks visible in agent briefings.
    """

    __tablename__ = "action_proposals"

    id = Column(GUID(), primary_key=True, default=uuid4)
    insight_id = Column(GUID(), nullable=False, index=True)  # FK to insights.id (loose)
    reflection_run_id = Column(GUID(), nullable=True)  # FK to reflection_runs.id (loose)
    project = Column(String(255), nullable=True, index=True)
    title = Column(String(255), nullable=False)  # Short action title
    action_blurb = Column(Text, nullable=False)  # What should be done
    status = Column(String(20), default="pending", index=True)  # pending, approved, declined
    decided_at = Column(DateTime, nullable=True)  # When user approved/declined
    task_description = Column(Text, nullable=True)  # Generated on approve (task body)
    created_at = Column(DateTime, server_default=func.now())


class ReflectionRun(AgentsBase):
    """A single reflection run — batch analysis of recent sessions to extract insights."""

    __tablename__ = "reflection_runs"

    id = Column(GUID(), primary_key=True, default=uuid4)
    project = Column(String(255), index=True, nullable=True)  # null = cross-project
    started_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="running")  # running, completed, failed

    # What was analyzed
    session_count = Column(Integer, default=0)
    window_hours = Column(Integer, default=24)

    # What was produced
    insights_created = Column(Integer, default=0)
    insights_merged = Column(Integer, default=0)
    insights_skipped = Column(Integer, default=0)

    # LLM metadata
    model = Column(String(100), nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)

    # Error tracking
    error = Column(Text, nullable=True)


class OikosWakeup(AgentsBase):
    """Durable record of a proactive Oikos wakeup opportunity."""

    __tablename__ = "oikos_wakeups"

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
        Index("ix_oikos_wakeups_owner_created", "owner_id", "created_at"),
        Index("ix_oikos_wakeups_trigger_status", "trigger_type", "status"),
    )
