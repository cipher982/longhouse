"""Agent session models for cross-provider session tracking.

These models store sessions from all AI coding assistants (Claude Code, Codex,
Gemini, Cursor, Oikos) in a provider-agnostic format.

The schema lives in the 'agents' schema (not 'zerg') to enable:
1. OSS users to run Zerg standalone without Life Hub
2. Cross-provider session tracking in a unified format
3. Session continuity for Claude Code --resume
"""

import os
from typing import TYPE_CHECKING
from uuid import UUID as PyUUID
from uuid import uuid4

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.types import CHAR
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    pass


class GUID(TypeDecorator):
    """Platform-independent GUID type.

    Uses PostgreSQL's UUID type for Postgres, stores as CHAR(36) for SQLite.
    Based on SQLAlchemy's TypeDecorator pattern for cross-database UUID support.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        elif dialect.name == "postgresql":
            return value if isinstance(value, PyUUID) else PyUUID(value)
        else:
            return str(value) if isinstance(value, PyUUID) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        elif isinstance(value, PyUUID):
            return value
        else:
            return PyUUID(value)


# Separate metadata for agents schema (isolated from main zerg schema)
# AGENTS_SCHEMA is None for SQLite (no schema support), "agents" for Postgres
_db_url = os.environ.get("DATABASE_URL", "")
AGENTS_SCHEMA = None if _db_url.startswith("sqlite") else "agents"
agents_metadata = MetaData(schema=AGENTS_SCHEMA)

# Separate Base class for agents schema models
AgentsBase = declarative_base(metadata=agents_metadata)


class AgentSession(AgentsBase):
    """A single AI coding session from any provider.

    Stores session-level metadata like project, provider, git context, and
    message counts. Each session has many events (messages, tool calls).
    """

    __tablename__ = "sessions"

    # Primary key - UUID allows federation and prevents collision
    # GUID TypeDecorator: UUID for Postgres, CHAR(36) for SQLite
    id = Column(GUID(), primary_key=True, default=uuid4)

    # Provider identification
    provider = Column(String(50), nullable=False, index=True)  # claude, codex, gemini, cursor, oikos

    # Environment classification (required - no default, caller must specify)
    environment = Column(String(20), nullable=False, index=True)  # production, development, test, e2e

    # Context
    project = Column(String(255), nullable=True, index=True)  # Project name (parsed from cwd)
    device_id = Column(String(255), nullable=True, index=True)  # Machine identifier
    cwd = Column(Text, nullable=True)  # Working directory
    git_repo = Column(String(500), nullable=True)  # Git remote URL
    git_branch = Column(String(255), nullable=True)  # Git branch name

    # Timing
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    # Counts (denormalized for fast queries)
    user_messages = Column(Integer, default=0)
    assistant_messages = Column(Integer, default=0)
    tool_calls = Column(Integer, default=0)

    # Provider-specific session ID (e.g., Claude Code session UUID from filename)
    provider_session_id = Column(String(255), nullable=True, index=True)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    events = relationship("AgentEvent", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_sessions_project_started", "project", "started_at"),
        Index("ix_sessions_provider_started", "provider", "started_at"),
    )


class AgentEvent(AgentsBase):
    """A single event within an AI session (message, tool call, etc.).

    Events are the granular units of a session transcript. They can be:
    - User messages (role='user')
    - Assistant messages (role='assistant')
    - Tool calls (role='assistant' with tool_name set)
    - Tool results (role='tool')
    - System messages (role='system')
    """

    __tablename__ = "events"

    # Primary key - Integer with BigInteger variant for Postgres (BIGSERIAL)
    # SQLite requires INTEGER PRIMARY KEY for auto-increment
    id = Column(Integer().with_variant(BigInteger, "postgresql"), primary_key=True, autoincrement=True)

    # Foreign key to session - GUID TypeDecorator handles UUID/String conversion
    # ForeignKey reference is dynamic based on schema (None for SQLite, "agents" for Postgres)
    _fk_ref = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    session_id = Column(
        GUID(),
        ForeignKey(_fk_ref, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Event content
    role = Column(String(20), nullable=False, index=True)  # user, assistant, tool, system
    content_text = Column(Text, nullable=True)  # Message text content

    # Tool call data (when role='assistant' and this is a tool call)
    tool_name = Column(String(100), nullable=True, index=True)  # e.g., 'Edit', 'Bash', 'Read'
    tool_input_json = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)  # Tool call parameters
    tool_output_text = Column(Text, nullable=True)  # Tool result (when role='tool')

    # Timing
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    # Deduplication (for incremental sync)
    source_path = Column(Text, nullable=True)  # Original file path (e.g., ~/.claude/projects/.../session.jsonl)
    source_offset = Column(BigInteger, nullable=True)  # Byte offset in source file
    event_hash = Column(String(64), nullable=True, index=True)  # SHA-256 of event content

    # Schema versioning for format evolution
    schema_version = Column(Integer, default=1)

    # Raw storage for lossless archiving (original JSONL line)
    raw_json = Column(Text, nullable=True)

    # Relationships
    session = relationship("AgentSession", back_populates="events")

    __table_args__ = (
        # Deduplication: prevent re-ingesting the same event
        Index(
            "ix_events_dedup",
            "session_id",
            "source_path",
            "source_offset",
            "event_hash",
            unique=True,
            postgresql_where=(source_path.isnot(None)),
            sqlite_where=(source_path.isnot(None)),
        ),
        Index("ix_events_session_timestamp", "session_id", "timestamp"),
        Index("ix_events_role_tool", "role", "tool_name"),
    )
