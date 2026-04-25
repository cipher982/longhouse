"""ActivityKit Live Activity push-token registrations for iOS."""

from uuid import uuid4

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.types import GUID


class APNSLiveActivityRegistration(Base):
    """A per-session ActivityKit push token for one Live Activity."""

    __tablename__ = "apns_live_activity_registrations"

    id = Column(GUID(), primary_key=True, default=uuid4)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String(64), nullable=False, index=True)
    activity_id = Column(String(255), nullable=False)
    push_token = Column(String(255), nullable=False)
    push_environment = Column(String(32), nullable=False, default="sandbox")
    app_build_id = Column(String(255), nullable=True)
    last_state_hash = Column(String(64), nullable=True)
    last_push_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "activity_id", name="uq_apns_live_activity_owner_activity"),
        UniqueConstraint("owner_id", "push_token", name="uq_apns_live_activity_owner_token"),
        Index("ix_apns_live_activity_owner_session", "owner_id", "session_id", "ended_at"),
    )
