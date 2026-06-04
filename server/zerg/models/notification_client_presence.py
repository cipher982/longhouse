"""Browser client presence for notification suppression policy."""

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.sql import func

from zerg.database import Base


class NotificationClientPresence(Base):
    """Last-seen foreground state for one notification-capable client."""

    __tablename__ = "notification_client_presence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    client_id = Column(String, nullable=False)
    client_type = Column(String, nullable=False, default="web")
    visible = Column(Boolean, nullable=False, default=False)
    route = Column(String, nullable=True)
    session_id = Column(String, nullable=True, index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "client_id", name="uq_notification_client_presence_owner_client"),
        Index("ix_notification_client_presence_owner_seen", "owner_id", "last_seen_at"),
        Index("ix_notification_client_presence_owner_visible_seen", "owner_id", "visible", "last_seen_at"),
    )
