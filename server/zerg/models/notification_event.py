"""Durable notification events for attention delivery and audit."""

from uuid import uuid4

from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.types import GUID


class NotificationEvent(Base):
    """One server-side notification decision for a user/session."""

    __tablename__ = "notification_events"

    id = Column(GUID(), primary_key=True, default=uuid4)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String(64), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    state_key = Column(String(255), nullable=True)
    collapse_key = Column(String(255), nullable=True, index=True)
    event_started_at = Column(DateTime(timezone=True), nullable=False)
    eligible_at = Column(DateTime(timezone=True), nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    dismissed_at = Column(DateTime(timezone=True), nullable=True)
    channel_results = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_notification_events_owner_session_type", "owner_id", "session_id", "event_type"),
        Index("ix_notification_events_owner_unresolved", "owner_id", "resolved_at"),
    )
