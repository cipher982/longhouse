"""Refresh token sessions for browser auth.

Each row represents one refresh token in a rotation chain. Tokens are grouped
into families (via ``family_id``) so that reuse detection can revoke an entire
lineage when a stolen token is replayed.

Auto-created via ``Base.metadata.create_all()`` — no Alembic required.
"""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.sql import func

from zerg.database import Base


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id = Column(Integer, primary_key=True)

    # SHA-256 hash of the opaque token value (never store raw tokens).
    token_hash = Column(String, unique=True, nullable=False, index=True)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # All tokens descended from the same login share a family_id.
    # Reuse detection revokes every token in the family.
    family_id = Column(String(36), nullable=False, index=True)

    # Points to the token this one replaced (NULL for the first in the family).
    parent_id = Column(Integer, ForeignKey("refresh_sessions.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())

    # Hard ceiling — token dies regardless of activity.
    absolute_expires_at = Column(DateTime, nullable=False)

    # Sliding window — token dies if unused for too long.
    idle_expires_at = Column(DateTime, nullable=False)

    # Set when this token is consumed by rotation (not revocation).
    used_at = Column(DateTime, nullable=True)

    # Set when the family is revoked (reuse detection or explicit logout).
    revoked_at = Column(DateTime, nullable=True)
