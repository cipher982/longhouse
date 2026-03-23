"""Connector model for external integration credentials."""

from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class Connector(Base):
    """External integration connector.

    Stores provider credentials/config for both inbound (triggers) and
    outbound (notifications/actions). This becomes the single source of
    truth – triggers reference a connector, and providers operate strictly
    via a connector id.
    """

    __tablename__ = "connectors"
    __table_args__ = (
        # Ensure a user cannot create duplicate connectors for the same
        # (type, provider) pair. Prevents accidental duplicates during
        # repeated connect flows.
        UniqueConstraint("owner_id", "type", "provider", name="uix_connector_owner_type_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Ownership – the user who created/owns this connector
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    owner = relationship("User", backref="connectors")

    # High-level type and provider identifier
    # Examples: type="email", provider="gmail" | provider="smtp"
    type = Column(String, nullable=False)
    provider = Column(String, nullable=False)

    # Provider-specific configuration (encrypted secrets, meta, watch info, etc.)
    config = Column(MutableDict.as_mutable(JSON), nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
