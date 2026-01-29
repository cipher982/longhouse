"""Device token model for per-device authentication.

Per-device tokens enable secure, revocable authentication for CLI tools
like the shipper. Each token is scoped to a user and can be individually
revoked if a device is compromised.

Note on expiry: Tokens do not time-expire. They remain valid indefinitely
until explicitly revoked via the API. This simplifies CLI UX (no re-auth
needed) while still allowing security response via revocation. Time-based
expiry can be added later if compliance or security policy requires it.
"""

from uuid import uuid4

from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from zerg.database import Base


class DeviceToken(Base):
    """A per-device authentication token.

    Tokens are issued during `zerg connect` and validated on each API call.
    The plain token is only shown once during creation; we store a SHA-256
    hash for validation.
    """

    __tablename__ = "device_tokens"

    # Primary key - UUID for uniqueness
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # Owner - the user this token belongs to
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Device identification
    device_id = Column(String(255), nullable=False)  # Hostname or user-provided name

    # Token hash - SHA-256 hash of the plain token (never store plain token)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    # Table constraints
    __table_args__ = (
        # Index for looking up tokens by owner + device
        Index("ix_device_tokens_owner_device", "owner_id", "device_id"),
    )

    @property
    def is_revoked(self) -> bool:
        """Return True if this token has been revoked."""
        return self.revoked_at is not None

    @property
    def is_valid(self) -> bool:
        """Return True if this token is valid (not revoked)."""
        return not self.is_revoked
