"""Session share records for explicit, revocable share links."""

from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import text
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.types import GUID


class SessionShare(Base):
    """A signed share-link grant for a single Longhouse session."""

    __tablename__ = "session_shares"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(GUID(), ForeignKey("sessions.id"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    note = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    revoked_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    access_count = Column(Integer, nullable=False, server_default=text("0"))
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (Index("ix_session_shares_session_creator", "session_id", "created_by_user_id"),)


class SessionShareEvent(Base):
    """Audit trail for session share lifecycle and access events."""

    __tablename__ = "session_share_events"

    id = Column(Integer, primary_key=True, index=True)
    share_id = Column(Integer, ForeignKey("session_shares.id"), nullable=False, index=True)
    session_id = Column(GUID(), ForeignKey("sessions.id"), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
