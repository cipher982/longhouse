from sqlalchemy import JSON

# SQLAlchemy core imports
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import backref
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

# Local helpers / enums
from zerg.database import Base

from .user import User  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Runners – User-owned execution infrastructure (Runners v1)
# ---------------------------------------------------------------------------


class Runner(Base):
    """User-owned runner daemon for executing commands.

    Runners connect outbound to the Longhouse platform and execute jobs
    on behalf of the user. This enables secure execution without backend
    access to user SSH keys.
    """

    __tablename__ = "runners"
    __table_args__ = (
        # Ensure unique runner names per owner
        UniqueConstraint("owner_id", "name", name="uix_runner_owner_name"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Ownership
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    owner = relationship("User", backref="runners")

    # Identity and configuration
    name = Column(String, nullable=False)  # User-editable, unique per owner
    availability_policy = Column(String, nullable=False, default="always_on")  # always_on|on_demand|ephemeral
    labels = Column(MutableDict.as_mutable(JSON), nullable=True)  # e.g. {"role": "laptop", "env": "prod"}
    capabilities = Column(
        MutableList.as_mutable(JSON), nullable=False, default=lambda: ["exec.readonly"]
    )  # e.g. ["exec.readonly"], ["exec.full", "docker"]

    # Connection state
    status = Column(String, nullable=False, default="offline")  # online|offline|revoked
    last_seen_at = Column(DateTime, nullable=True)

    # Authentication
    auth_secret_hash = Column(String, nullable=False)  # SHA256 hash of runner secret

    # Metadata from runner (hostname, os, arch, version, docker_available, etc.)
    runner_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    jobs = relationship("RunnerJob", back_populates="runner", cascade="all, delete-orphan")


class RunnerEnrollToken(Base):
    """One-time enrollment token for registering a new runner.

    Tokens are created by the API and consumed during runner registration.
    They expire after a short TTL (e.g. 10 minutes) for security.
    """

    __tablename__ = "runner_enroll_tokens"

    id = Column(Integer, primary_key=True, index=True)

    # Ownership
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    owner = relationship("User", backref="runner_enroll_tokens")

    # Token data
    token_hash = Column(String, nullable=False, unique=True, index=True)  # SHA256 hash
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)  # Set when token is consumed

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class RunnerJob(Base):
    """Execution job for a runner.

    Represents a single command execution request sent to a runner.
    Includes audit trail and output truncation for safety.
    """

    __tablename__ = "runner_jobs"

    id = Column(String, primary_key=True)  # UUID as string

    # Ownership and correlation
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    owner = relationship("User", backref="runner_jobs")

    correlation_id = Column(String, nullable=True, index=True)
    run_id = Column(String, nullable=True)  # Link to run context

    # Runner assignment
    runner_id = Column(Integer, ForeignKey("runners.id", ondelete="CASCADE"), nullable=False, index=True)
    runner = relationship("Runner", back_populates="jobs")

    # Job specification
    command = Column(Text, nullable=False)
    timeout_secs = Column(Integer, nullable=False)

    # Execution state
    status = Column(String, nullable=False, default="queued")  # queued|running|success|failed|timeout|canceled
    exit_code = Column(Integer, nullable=True)

    # Timing
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    # Output (truncated/capped for safety)
    stdout_trunc = Column(Text, nullable=True)
    stderr_trunc = Column(Text, nullable=True)

    # Error handling
    error = Column(Text, nullable=True)

    # Future: file upload support
    artifacts = Column(MutableDict.as_mutable(JSON), nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class RunnerHealthIncident(Base):
    """Durable runner health incident for deduped alerts and wakeups."""

    __tablename__ = "runner_health_incidents"
    __table_args__ = (
        Index("ix_runner_health_incidents_runner_status", "runner_id", "status"),
        Index("ix_runner_health_incidents_owner_status", "owner_id", "status"),
        Index("ix_runner_health_incidents_opened", "opened_at"),
    )

    id = Column(Integer, primary_key=True, index=True)

    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    owner = relationship("User", backref=backref("runner_health_incidents", cascade="all, delete-orphan"))

    runner_id = Column(Integer, ForeignKey("runners.id", ondelete="CASCADE"), nullable=False, index=True)
    runner = relationship("Runner", backref=backref("health_incidents", cascade="all, delete-orphan"))

    incident_type = Column(String, nullable=False, default="offline")
    status = Column(String, nullable=False, default="open")  # open|resolved
    reason_code = Column(String, nullable=False)
    summary = Column(Text, nullable=True)
    context = Column(MutableDict.as_mutable(JSON), nullable=True)

    opened_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_observed_at = Column(DateTime, server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    alert_sent_at = Column(DateTime, nullable=True)
    alert_claimed_at = Column(DateTime, nullable=True)
    alert_channel = Column(String, nullable=True)
    alert_count = Column(Integer, nullable=False, default=0)

    wakeup_sent_at = Column(DateTime, nullable=True)
    wakeup_count = Column(Integer, nullable=False, default=0)


# ---------------------------------------------------------------------------
# Email Secrets – Encrypted SES credentials for instance email
# ---------------------------------------------------------------------------


class EmailSecret(Base):
    """Encrypted email config secret (SES creds + sender/recipient).

    One row per (owner, key) for the well-known email keys defined in
    ``zerg.shared.email`` (AWS_SES_ACCESS_KEY_ID, FROM_EMAIL, etc.).
    Resolution order: DB first, env var fallback (self-hosted compatibility).
    """

    __tablename__ = "email_secrets"
    __table_args__ = (UniqueConstraint("owner_id", "key", name="uix_email_secret_owner_key"),)

    id = Column(Integer, primary_key=True, index=True)

    owner_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    key = Column(String(255), nullable=False)  # e.g. "AWS_SES_ACCESS_KEY_ID"
    encrypted_value = Column(Text, nullable=False)  # Fernet AES-GCM
    description = Column(String(500), nullable=True)  # Optional hint for UI

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    owner = relationship("User", backref="email_secrets")
