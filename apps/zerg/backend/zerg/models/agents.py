"""Agent session models for cross-provider session tracking.

These models store sessions from all AI coding assistants (Claude Code, Codex,
Gemini, Cursor, Oikos) in a provider-agnostic format.

For SQLite-only mode, these tables live in the main database.
"""

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import LargeBinary
from sqlalchemy import MetaData
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.models.types import GUID

if TYPE_CHECKING:
    pass


# SQLite-only: no schema support, tables live in main database
AGENTS_SCHEMA = None
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

    # Pre-computed summary (generated async after ingest)
    summary = Column(Text, nullable=True)  # 2-4 sentence quick summary
    summary_title = Column(String(200), nullable=True)  # Short title for briefing
    summary_event_count = Column(Integer, server_default=text("0"))  # Events covered by current summary (legacy count-based cursor)
    last_summarized_event_id = Column(Integer, nullable=True)  # ID of last AgentEvent included in summary (efficient cursor)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Embedding tracking (1 = needs embedding, 0 = done; SQLite has no bool)
    needs_embedding = Column(Integer, server_default=text("1"))

    # Reflection tracking — stamped when session has been analyzed by reflection service
    reflected_at = Column(DateTime(timezone=True), nullable=True)

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

    # Primary key - INTEGER for SQLite auto-increment
    id = Column(Integer, primary_key=True, autoincrement=True)

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
    tool_input_json = Column(JSON(), nullable=True)  # Tool call parameters
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


class AgentHeartbeat(AgentsBase):
    """Periodic health check from a running engine daemon.

    Stores the latest heartbeat per device, with history retained for 30 days.
    Auto-created via AgentsBase.metadata.create_all() — no Alembic required.
    """

    __tablename__ = "agent_heartbeats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String(255), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Engine version
    version = Column(String(50), nullable=True)

    # Last successful ship timestamp
    last_ship_at = Column(DateTime(timezone=True), nullable=True)

    # Stats
    spool_pending = Column(Integer, default=0)
    parse_errors_1h = Column(Integer, default=0)
    consecutive_failures = Column(Integer, default=0)
    disk_free_bytes = Column(BigInteger, default=0)
    is_offline = Column(Integer, default=0)  # 0/1 (SQLite has no bool)

    # Full payload for forward compatibility
    raw_json = Column(Text, nullable=True)

    __table_args__ = (Index("ix_heartbeats_device_received", "device_id", "received_at"),)


class SessionEmbedding(AgentsBase):
    """Embedding vectors for session search and recall."""

    __tablename__ = "session_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        GUID(),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Embedding classification
    kind = Column(String(20), nullable=False)  # 'session' or 'turn'
    chunk_index = Column(Integer, default=-1)  # -1 = session-level, >=0 = turn index

    # Event mapping (for recall context window retrieval)
    event_index_start = Column(Integer, nullable=True)
    event_index_end = Column(Integer, nullable=True)

    # Model tracking (for re-embedding if model changes)
    model = Column(String(128), nullable=False)
    dims = Column(Integer, nullable=False)

    # The vector (numpy float32 serialized to bytes)
    embedding = Column(LargeBinary, nullable=False)

    # Dedup / versioning
    content_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "kind", "chunk_index", "model", name="uq_session_emb"),
        Index("ix_session_emb_session", "session_id"),
        Index("ix_session_emb_kind", "kind", "chunk_index"),
    )


class SessionPresence(AgentsBase):
    """Real-time presence state for an active Claude Code session.

    Written by the Stop/UserPromptSubmit/PreToolUse/PostToolUse hooks via
    POST /api/agents/presence. Rows are upserted (one per session_id) so the
    table stays small. Stale rows (updated_at > 10 min ago) are treated as
    gone and can be pruned periodically.

    States:
        thinking  — UserPromptSubmit fired; LLM is generating tokens
        running   — PreToolUse fired; a tool is actively executing
        idle      — Stop fired; session complete, waiting for next prompt
    """

    __tablename__ = "session_presence"

    session_id = Column(String(255), primary_key=True)
    state = Column(String(32), nullable=False)  # thinking | running | idle
    tool_name = Column(String(128), nullable=True)  # set when state=running
    cwd = Column(String(512), nullable=True)
    project = Column(String(255), nullable=True)  # basename(cwd)
    provider = Column(String(64), nullable=False, default="claude")
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (Index("ix_presence_updated", "updated_at"),)


class SessionTask(AgentsBase):
    """Durable task queue for post-ingest background work (summary + embeddings).

    Replaces FastAPI BackgroundTasks for summary/embedding generation so tasks
    survive process restarts. Worker polls this table and retries failures.

    task_type: 'summary' | 'embedding'
    status:    'pending' | 'running' | 'done' | 'failed'
    """

    __tablename__ = "session_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id = Column(String(36), nullable=False)
    task_type = Column(String(32), nullable=False)  # summary | embedding
    status = Column(String(16), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, nullable=False, server_default=text("3"))
    error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # Fast lookup of pending tasks ordered by creation time
        Index("ix_session_tasks_status_created", "status", "created_at"),
        # Index for dedup check: find existing active tasks per session
        Index("ix_session_tasks_session_type_status", "session_id", "task_type", "status"),
    )
