"""CourseEvent model for durable event streaming."""

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class CourseEvent(Base):
    """Represents a single event in a course's lifecycle.

    This table provides durable storage for all concierge/commis events,
    enabling SSE streams to reconnect and replay missed events. All events
    that were previously only published to the EventBus are now also persisted
    here for full audit trail and resumable streaming.

    Key features:
    - Sequential ordering via auto-incrementing id (BigSerial is monotonic)
    - JSONB payload for flexible event data
    - Cascade delete when run is deleted
    - Efficient indexes for replay queries
    """

    __tablename__ = "course_events"

    # SQLite only auto-increments reliably when the PK column is exactly INTEGER.
    # Use a dialect variant so Postgres keeps BigInt/BigSerial semantics.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True, autoincrement=True)

    # Foreign keys -------------------------------------------------------
    course_id = Column(Integer, ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)

    # Event metadata -----------------------------------------------------
    event_type = Column(String(50), nullable=False, index=True)  # concierge_started, commis_complete, etc.

    # Event payload ------------------------------------------------------
    payload = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)  # Full event data (JSON-serializable)

    # Timestamps ---------------------------------------------------------
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Relationships ------------------------------------------------------
    course = relationship("Course", backref="events")

    # Table constraints --------------------------------------------------
    __table_args__ = (
        Index("idx_course_events_course_id", "course_id"),
        Index("idx_course_events_created_at", "created_at"),
        Index("idx_course_events_type", "event_type"),
    )
