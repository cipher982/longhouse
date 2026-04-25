"""Server-side debounce state for WidgetKit refresh pushes."""

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.sql import func

from zerg.database import Base


class APNSWidgetPushState(Base):
    """Last widget set fingerprint pushed for a user."""

    __tablename__ = "apns_widget_push_states"

    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    state_hash = Column(String(64), nullable=True)
    last_push_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
