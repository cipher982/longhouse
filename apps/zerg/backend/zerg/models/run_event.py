"""RunEvent model for durable event streaming."""

from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class RunEvent(Base):
    """Represents a single event in a run's lifecycle.

    This table provides durable storage for all oikos/commis events,
    enabling SSE streams to reconnect and replay missed events. All events
    that were previously only published to the EventBus are now also persisted
    here for full audit trail and resumable streaming.

    Key features:
    - Sequential ordering via auto-incrementing id
    - JSON payload for flexible event data
    - Cascade delete when run is deleted
    - Efficient indexes for replay queries
    """

    __tablename__ = "run_events"

    # SQLite requires INTEGER PRIMARY KEY for auto-increment
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # Foreign keys -------------------------------------------------------
    run_id = Column(Integer, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)

    # Event metadata -----------------------------------------------------
    event_type = Column(String(50), nullable=False, index=True)  # oikos_started, commis_complete, etc.

    # Event payload ------------------------------------------------------
    payload = Column(JSON(), nullable=False)  # Full event data (JSON-serializable)

    # Timestamps ---------------------------------------------------------
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Relationships ------------------------------------------------------
    run = relationship("Run", backref="events")

    # Table constraints --------------------------------------------------
    __table_args__ = (
        Index("idx_run_events_run_id", "run_id"),
        Index("idx_run_events_created_at", "created_at"),
        Index("idx_run_events_type", "event_type"),
    )
