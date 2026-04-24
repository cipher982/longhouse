"""APNs device registrations for the native iOS companion."""

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


class APNSDeviceRegistration(Base):
    """A browser-authenticated iOS device that can receive APNs pushes."""

    __tablename__ = "apns_device_registrations"

    id = Column(GUID(), primary_key=True, default=uuid4)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    platform = Column(String(32), nullable=False, default="ios")
    device_token = Column(String(255), nullable=False)
    push_environment = Column(String(32), nullable=False, default="sandbox")
    app_build_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "device_token", name="uq_apns_registration_owner_token"),
        Index("ix_apns_registration_owner_platform", "owner_id", "platform"),
    )
