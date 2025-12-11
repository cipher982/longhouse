"""Sync operations model for conversation synchronization.

This model stores sync operations from Jarvis clients to enable
offline-first conversation sync with idempotent push operations.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base


class SyncOperation(Base):
    """A sync operation from a Jarvis client.

    Stores conversation sync operations (messages, edits, deletions) with
    idempotent push semantics. Clients generate unique op_id values to
    prevent duplicate operations when retrying.
    """

    __tablename__ = "sync_operations"

    id = Column(Integer, primary_key=True, index=True)

    # Client-generated ID for idempotency
    op_id = Column(String, unique=True, nullable=False, index=True)

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
