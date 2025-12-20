"""Sync operations model for conversation synchronization.

This model stores sync operations from Jarvis clients to enable
offline-first conversation sync with idempotent push operations.
"""

from sqlalchemy import JSON
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class SyncOperation(Base):
    """A sync operation from a Jarvis client.

    Stores conversation sync operations (messages, edits, deletions) with
    idempotent push semantics. Clients generate unique op_id values to
    prevent duplicate operations when retrying.

    Note: op_id uniqueness is scoped per-user, not globally. This allows
    different users to have the same op_id without conflict.
    """

    __tablename__ = "sync_operations"
    __table_args__ = (
        # op_id uniqueness is per-user for multi-tenant correctness
        UniqueConstraint("user_id", "op_id", name="uq_sync_operations_user_op"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Client-generated ID for idempotency (unique per user, not globally)
    op_id = Column(String, nullable=False, index=True)

    # Ownership
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    user = relationship("User", backref="sync_operations")

    # Operation details
    type = Column(String, nullable=False)  # e.g., "message", "conversation", "edit"
    body = Column(JSON, nullable=False)  # Operation payload

    # Client-side ordering
    lamport = Column(Integer, nullable=False)  # Lamport timestamp for ordering
    ts = Column(DateTime, nullable=False)  # Client timestamp

    # Server tracking
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
