"""AgentRun model for execution tracking."""

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger


class AgentRun(Base):
    """Represents a single *execution* of an Agent.

    An AgentRun is created whenever an agent task is executed either manually,
    via the scheduler or through an external trigger.  It references the
    underlying *Thread* that captures the chat transcript but keeps
    additional execution-level metadata (status, timing, cost, etc.) that is
    cumbersome to derive from the chat model alone.
    """

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign keys -------------------------------------------------------
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    thread_id = Column(Integer, ForeignKey("agent_threads.id"), nullable=False)
    # Durable runs v2.2: Link continuation runs to original deferred run
    continuation_of_run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=True)

    # Observability ------------------------------------------------------
    # Phase 1: Correlation ID for tracing requests end-to-end (chat-observability-eval)
    correlation_id = Column(String, nullable=True, index=True)

    # Trace ID for end-to-end debugging (UUID, propagated to workers and LLM audit)
    trace_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Model used for this run (for continuation inheritance)
    model = Column(String(100), nullable=True)

    # Reasoning effort used for this run (for continuation inheritance)
    # Values: none, low, medium, high
    reasoning_effort = Column(String(20), nullable=True)

    # Message ID (UUID) assigned to the assistant message in supervisor_started event.
    # Used by continuation runs to look up the original message's ID for
    # continuation_of_message_id (schema requires UUID, not sentinel string).
    assistant_message_id = Column(String(36), nullable=True)

    # Lifecycle ----------------------------------------------------------
    status = Column(
        SAEnum(RunStatus, native_enum=False, name="run_status_enum"),
        default=RunStatus.QUEUED.value,
        nullable=False,
    )  # queued → running → success|failed
    trigger = Column(
        SAEnum(RunTrigger, native_enum=False, name="run_trigger_enum"),
        default=RunTrigger.MANUAL.value,
        nullable=False,
    )  # manual / schedule / api

    # Timing -------------------------------------------------------------
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Usage --------------------------------------------------------------
    total_tokens = Column(Integer, nullable=True)
    total_cost_usd = Column(Float, nullable=True)

    # Failure ------------------------------------------------------------
    error = Column(Text, nullable=True)
    cancel_reason = Column(Text, nullable=True)

    # Summary ------------------------------------------------------------
    # Brief summary of the run for Jarvis Task Inbox (first assistant response or truncated output)
    summary = Column(Text, nullable=True)

    # Timestamps ---------------------------------------------------------
    # Note: nullable=True for SQLite compatibility with existing tables
    # New rows will have defaults, existing rows backfilled by migration
    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=True)

    # Relationships ------------------------------------------------------
    agent = relationship("Agent", back_populates="runs")
    thread = relationship("Thread", backref="runs")
    # Durable runs v2.2: Self-referential relationship for continuation chains
    continued_from = relationship("AgentRun", remote_side=[id], backref="continuations")

    # Table constraints --------------------------------------------------
    __table_args__ = (
        # Durable runs v2.2: Ensure only one continuation per original run (idempotency)
        # Allows multiple rows with NULL continuation_of_run_id (non-continuation runs)
        Index(
            "ix_agent_runs_unique_continuation",
            continuation_of_run_id,
            unique=True,
            postgresql_where=(continuation_of_run_id.isnot(None)),
            sqlite_where=(continuation_of_run_id.isnot(None)),
        ),
    )
