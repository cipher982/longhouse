"""Work tracking models — insights and file reservations.

These models support agent infrastructure: tracking learnings across sessions
and preventing file edit conflicts in multi-agent workflows.
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
from sqlalchemy import text
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


class FileReservation(AgentsBase):
    """A file reservation to prevent edit conflicts in multi-agent workflows."""

    __tablename__ = "file_reservations"

    id = Column(GUID(), primary_key=True, default=uuid4)
    file_path = Column(Text, nullable=False)
    project = Column(String(255), nullable=False, server_default="")  # non-null, empty = global
    agent = Column(String(255), nullable=False, default="claude")
    reason = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    released_at = Column(DateTime, nullable=True)  # NULL = active
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        # Only one active reservation per file+project
        Index(
            "ix_reservation_active",
            "file_path",
            "project",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
        ),
    )
