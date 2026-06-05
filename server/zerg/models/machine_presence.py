"""Latest coarse local presence reported by a Machine Agent."""

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.sql import func

from zerg.models.agents import AgentsBase


class MachinePresence(AgentsBase):
    """Latest privacy-scoped local idle state for one owner/device."""

    __tablename__ = "machine_presence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    device_id = Column(String(255), nullable=False)
    state = Column(String(32), nullable=False)
    source = Column(String(64), nullable=False, default="unknown")
    idle_seconds = Column(Integer, nullable=True)
    measured_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "device_id", name="uq_machine_presence_owner_device"),
        Index("ix_machine_presence_owner_received", "owner_id", "received_at"),
        Index("ix_machine_presence_owner_state_received", "owner_id", "state", "received_at"),
    )
