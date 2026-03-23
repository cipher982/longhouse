"""Loop PWA web-push subscriptions."""

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from zerg.database import Base


class LoopPushSubscription(Base):
    """Browser push subscription for the Loop PWA."""

    __tablename__ = "loop_push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Hash the endpoint for stable lookup without indexing a huge raw URL.
    endpoint_hash = Column(String(64), nullable=False)
    subscription_json = Column(MutableDict.as_mutable(JSON), nullable=False)

    install_id = Column(String(255), nullable=True)
    user_agent = Column(Text, nullable=True)

    last_push_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("endpoint_hash", name="uq_loop_push_subscriptions_endpoint_hash"),
        Index("ix_loop_push_subscriptions_owner_revoked", "owner_id", "revoked_at"),
    )
