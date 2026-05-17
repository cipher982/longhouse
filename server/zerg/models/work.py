"""Work tracking models — insights, incidents, and wakeups.

These models support agent infrastructure: tracking reusable learnings across
sessions, storing tenant-local operational incidents, and recording proactive
runner wakeups.
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
OPERATIONAL_INCIDENT_STATUS_OPEN = "open"
OPERATIONAL_INCIDENT_STATUS_RESOLVED = "resolved"


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


class OperationalIncident(AgentsBase):
    """Durable tenant-local ops incident, separate from curated memory."""

    __tablename__ = "operational_incidents"
    __table_args__ = (
        Index("ix_operational_incidents_dedupe_status", "dedupe_key", "status"),
        Index("ix_operational_incidents_source_status", "source", "status"),
        Index("ix_operational_incidents_last_observed", "last_observed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_type = Column(String(64), nullable=False, index=True)
    source = Column(String(64), nullable=False, index=True)
    dedupe_key = Column(String(255), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=OPERATIONAL_INCIDENT_STATUS_OPEN)
    summary = Column(Text, nullable=False)
    context = Column(JSON, nullable=True)
    opened_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_observed_at = Column(DateTime, server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class OpsWatchObservation(AgentsBase):
    """Append-only raw operational observation for the AI watchman."""

    __tablename__ = "ops_watch_observations"
    __table_args__ = (
        Index("ix_ops_watch_observations_source_observed", "source", "observed_at"),
        Index("ix_ops_watch_observations_entity_observed", "entity_type", "entity_id", "observed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    observed_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    window_start_at = Column(DateTime, nullable=True, index=True)
    window_end_at = Column(DateTime, nullable=True, index=True)
    entity_type = Column(String(32), nullable=False, index=True)
    entity_id = Column(String(255), nullable=False, index=True)
    source = Column(String(64), nullable=False, index=True)
    payload_json = Column(JSON, nullable=True)
    payload_text = Column(Text, nullable=True)


class OpsWatchRun(AgentsBase):
    """Durable record of one AI watchman analysis run."""

    __tablename__ = "ops_watch_runs"
    __table_args__ = (
        Index("ix_ops_watch_runs_started", "started_at"),
        Index("ix_ops_watch_runs_status_started", "status", "started_at"),
        Index("ix_ops_watch_runs_analysis_started", "analysis_status", "started_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running", index=True)  # running, success, skipped, error
    analysis_status = Column(String(20), nullable=True, index=True)  # normal, watch, critical, skipped
    model = Column(String(100), nullable=True)
    prompt_version = Column(String(64), nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    reasoning_tokens = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Float, nullable=True)
    usage_json = Column(JSON, nullable=True)
    result_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)


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
