"""AgentRunEvent model for durable event streaming."""

from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class AgentRunEvent(Base):
    """Represents a single event in an agent run's lifecycle.

    This table provides durable storage for all supervisor/worker events,
    enabling SSE streams to reconnect and replay missed events. All events
    that were previously only published to the EventBus are now also persisted
    here for full audit trail and resumable streaming.

    Key features:
    - Sequential ordering within a run (via sequence column)
    - JSONB payload for flexible event data
    - Cascade delete when run is deleted
    - Efficient indexes for replay queries
    """

    __tablename__ = "agent_run_events"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)

    # Foreign keys -------------------------------------------------------
    run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False)

    # Event metadata -----------------------------------------------------
    event_type = Column(String(50), nullable=False, index=True)  # supervisor_started, worker_complete, etc.
    sequence = Column(Integer, nullable=False)  # Monotonic sequence within run for ordering

    # Event payload ------------------------------------------------------
    payload = Column(JSONB, nullable=False)  # Full event data (JSON-serializable)

    # Timestamps ---------------------------------------------------------
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Relationships ------------------------------------------------------
    run = relationship("AgentRun", backref="events")

    # Table constraints --------------------------------------------------
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="agent_run_events_unique_seq"),
        Index("idx_run_events_run_id", "run_id"),
        Index("idx_run_events_created_at", "created_at"),
        Index("idx_run_events_type", "event_type"),
    )
