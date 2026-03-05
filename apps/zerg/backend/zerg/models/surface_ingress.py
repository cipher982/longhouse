"""Ingress idempotency claims for surface events."""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class SurfaceIngressClaim(Base):
    """Claim row for one inbound surface event idempotency key."""

    __tablename__ = "surface_ingress_claims"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "surface_id",
            "dedupe_key",
            name="uix_surface_ingress_owner_surface_dedupe",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    surface_id = Column(String(64), nullable=False, index=True)
    dedupe_key = Column(String(255), nullable=False)
    conversation_id = Column(String(255), nullable=False)
    source_event_id = Column(String(255), nullable=True)
    source_message_id = Column(String(255), nullable=True)
    claimed_at = Column(DateTime, nullable=False, server_default=func.now())

    owner = relationship("User", backref="surface_ingress_claims")
