"""Agent session models for cross-provider session tracking.

These models store sessions from AI coding assistants (Claude Code, Codex,
Antigravity, legacy Gemini, Cursor) in a provider-agnostic format.

For SQLite-only mode, these tables live in the main database.
"""

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import LargeBinary
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.types import GUID

if TYPE_CHECKING:
    pass


# SQLite-only: no schema support, tables live in main database.
# `AgentsBase` is retained as an alias for `Base` so existing imports keep
# working — there is one declarative base / metadata for the whole app.
AGENTS_SCHEMA = None
AgentsBase = Base
agents_metadata = Base.metadata


class AgentSession(AgentsBase):
    """A single AI coding session from any provider.

    Stores session-level metadata like project, provider, git context, and
    message counts. Each session has many events (messages, tool calls).
    """

    __tablename__ = "sessions"

    # Primary key - UUID allows federation and prevents collision.
    # GUID stores UUIDs as CHAR(36) in SQLite.
    id = Column(GUID(), primary_key=True, default=uuid4)

    # Provider identification
    provider = Column(String(50), nullable=False, index=True)  # claude, codex, antigravity, opencode, cursor

    # Environment classification (required - no default, caller must specify)
    environment = Column(String(20), nullable=False, index=True)  # production, development, test, e2e

    # Context
    project = Column(String(255), nullable=True, index=True)  # Project name (parsed from cwd)
    device_id = Column(String(255), nullable=True, index=True)  # Machine identifier
    device_name = Column(String(255), nullable=True)  # Human-friendly device label (e.g. "laptop", "demo-machine")
    cwd = Column(Text, nullable=True)  # Working directory
    git_repo = Column(String(500), nullable=True)  # Git remote URL
    git_branch = Column(String(255), nullable=True)  # Git branch name

    # Timing
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Counts (denormalized for fast queries)
    user_messages = Column(Integer, default=0)
    assistant_messages = Column(Integer, default=0)
    tool_calls = Column(Integer, default=0)

    summary = Column(Text, nullable=True)
    summary_title = Column(String(255), nullable=True)
    # Frozen, write-once AI title for the timeline card. It is owned by the
    # first-durable-user-message title pipeline; summaries may drift but must
    # never claim or replace this field. See services/session_title.py.
    anchor_title = Column(String(255), nullable=True)
    # Durable retry evidence for the AI title obligation. A non-null retry time
    # means an eligible session is title debt, even when a display fallback is
    # available from project or prompt context.
    title_attempt_count = Column(Integer, nullable=False, server_default=text("0"))
    title_last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    title_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    title_last_error = Column(String(128), nullable=True)
    first_user_message_preview = Column(Text, nullable=True)
    last_visible_text_preview = Column(Text, nullable=True)
    last_user_message_preview = Column(Text, nullable=True)
    last_assistant_message_preview = Column(Text, nullable=True)
    summary_event_count = Column(Integer, server_default=text("0"))
    last_summarized_event_id = Column(Integer, nullable=True)

    # Monotonic transcript generation for replay-safe downstream work.
    transcript_revision = Column(Integer, nullable=False, server_default=text("0"))
    summary_revision = Column(Integer, nullable=False, server_default=text("0"))
    embedding_revision = Column(Integer, nullable=False, server_default=text("0"))

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Embedding tracking (1 = needs embedding, 0 = done; SQLite has no bool)
    needs_embedding = Column(Integer, server_default=text("1"))

    # Derived projection tracking (1 = counts/turn projections need async
    # catch-up, 0 = current enough for read surfaces). Archive ingest sets this
    # instead of rebuilding expensive projections on the hot shipping path.
    needs_projection = Column(Integer, nullable=False, server_default=text("0"))

    # User-driven bucket classification (set via POST /sessions/{id}/action)
    # active (default) | parked | snoozed | archived
    user_state = Column(String(20), nullable=False, server_default=text("'active'"))
    user_state_at = Column(DateTime(timezone=True), nullable=True)

    # Session identity kernel — primary thread pointer.
    # Kept nullable for now: legacy ingest paths and a number of tests
    # create AgentSession rows before the kernel thread is materialized.
    # ``ensure_primary_thread`` backfills this on the next write touch.
    primary_thread_id = Column(GUID(), nullable=True, index=True)

    # APNS attention-push debounce state. The cleanup landed kernel rows
    # for control truth; these two fields are durable per-session debounce
    # state for outbound push notifications and intentionally live here
    # rather than in a kernel table.
    last_attention_push_at = Column(DateTime(timezone=True), nullable=True)
    last_attention_push_state = Column(String(40), nullable=True)

    # Per-session loop mode (assist/autopilot). Durable user-facing setting,
    # not control-plane state — kernel rows do not encode it. Stored as a
    # plain column so PATCH /loop-mode survives restart and refresh.
    loop_mode = Column(String(32), nullable=False, default="assist", server_default=text("'assist'"))
    notification_muted = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    # Launch-origin classification for Longhouse-owned automation. This is not
    # provider lineage: provider subagents/forks still live in session_threads
    # and session_edges. V1 uses "hatch_automation" to keep delegated Hatch
    # one-shots archived but hidden from default human timelines.
    origin_kind = Column(String(64), nullable=True, index=True)
    hidden_from_default_timeline = Column(Integer, nullable=False, server_default=text("0"))
    launch_actor = Column(String(32), nullable=True, index=True)
    launch_surface = Column(String(32), nullable=True, index=True)
    # Managed permission policy: "bypass" (default, autonomous/skip-permissions) or
    # "remote_approve" (pause on permission prompts, answerable via Longhouse). Has
    # a server_default so _auto_add_missing_columns adds it without a migrator.
    permission_mode = Column(String(32), nullable=False, default="bypass", server_default=text("'bypass'"))

    # Summary reconciler distributed lock: prevents multiple Runtime Host
    # replicas from concurrently calling the LLM for the same session.
    # See generate_summary_impl in services/session_summaries.py.
    summary_lock_instance = Column(String(64), nullable=True)
    summary_lock_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    branches = relationship("AgentSessionBranch", back_populates="session", cascade="all, delete-orphan")
    events = relationship("AgentEvent", back_populates="session", cascade="all, delete-orphan")
    source_lines = relationship("AgentSourceLine", back_populates="session", cascade="all, delete-orphan")
    live_preview = relationship(
        "SessionLivePreview",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_sessions_project_started", "project", "started_at"),
        Index("ix_sessions_provider_started", "provider", "started_at"),
    )


class AgentSessionBranch(AgentsBase):
    """Branch metadata for rewind-aware session projections."""

    __tablename__ = "session_branches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    _fk_ref = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    session_id = Column(
        GUID(),
        ForeignKey(_fk_ref, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_branch_id = Column(Integer, nullable=True)
    branched_at_source_path = Column(Text, nullable=True)
    branched_at_offset = Column(BigInteger, nullable=True)
    branch_reason = Column(String(32), nullable=False, server_default=text("'root'"))
    is_head = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("AgentSession", back_populates="branches")

    __table_args__ = (
        Index("ix_session_branches_session_created", "session_id", "created_at"),
        Index(
            "ix_session_branches_head",
            "session_id",
            unique=True,
            postgresql_where=(is_head == 1),
            sqlite_where=(is_head == 1),
        ),
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

    # Foreign key to session - GUID TypeDecorator handles UUID/String conversion.
    _fk_ref = "sessions.id"
    session_id = Column(
        GUID(),
        ForeignKey(_fk_ref, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Session identity kernel — kept nullable for now: legacy ingest paths
    # write events before the kernel thread is materialized.
    thread_id = Column(GUID(), nullable=True, index=True)

    # Event content
    role = Column(String(20), nullable=False, index=True)  # user, assistant, tool, system
    content_text = Column(Text, nullable=True)  # Message text content

    # Tool call data (when role='assistant' and this is a tool call)
    tool_name = Column(String(100), nullable=True, index=True)  # e.g., 'Edit', 'Bash', 'Read'
    tool_input_json = Column(JSON(), nullable=True)  # Tool call parameters
    tool_output_text = Column(Text, nullable=True)  # Tool result (when role='tool')
    # Cross-provider linkage: matches tool_use.id (call) to tool_result.tool_use_id (result)
    # Enables deterministic pairing of calls and results; None for non-tool events.
    tool_call_id = Column(String(255), nullable=True)

    # Timing
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)

    # Deduplication (for incremental sync)
    source_path = Column(Text, nullable=True)  # Original file path (e.g., ~/.claude/projects/.../session.jsonl)
    source_offset = Column(BigInteger, nullable=True)  # Byte offset in source file
    event_hash = Column(String(64), nullable=True, index=True)  # SHA-256 of event content
    branch_id = Column(Integer, nullable=True, index=True)  # Session branch head at ingest time

    # Schema versioning for format evolution
    schema_version = Column(Integer, default=1)

    # Raw storage for lossless archiving (original JSONL line)
    # codec=0: plain TEXT in raw_json; codec=1: zstd BLOB in raw_json_z (raw_json is NULL)
    raw_json = Column(Text, nullable=True)
    raw_json_z = Column(LargeBinary, nullable=True)
    raw_json_codec = Column(Integer, nullable=False, server_default=text("0"))
    # Raw line uuid from Claude/Codex/Antigravity/Gemini event streams.
    event_uuid = Column(String(255), nullable=True, index=True)
    parent_event_uuid = Column(String(255), nullable=True, index=True)  # Raw parent linkage id (Claude parentUuid)
    event_origin = Column(String(32), nullable=False, server_default=text("'durable'"), index=True)
    # Structured compaction-boundary marker, parsed from the raw line at ingest.
    # Values: 'summary', 'compact_boundary', 'microcompact_boundary', or NULL.
    # Lets active-context projection find boundaries without decoding raw_json at
    # request time, so raw payloads can move to the archive.
    compaction_kind = Column(String(32), nullable=True, index=True)
    provisional_state = Column(String(32), nullable=True, index=True)
    provisional_key = Column(String(512), nullable=True)
    provisional_cursor = Column(String(512), nullable=True)
    provisional_seq = Column(Integer, nullable=True)
    provisional_complete = Column(Integer, nullable=False, server_default=text("0"))
    reconciled_event_id = Column(Integer, nullable=True)

    # Relationships
    session = relationship("AgentSession", back_populates="events")

    __table_args__ = (
        # Deduplication: prevent re-ingesting the same event
        Index(
            "ix_events_dedup",
            "session_id",
            "branch_id",
            "source_path",
            "source_offset",
            "event_hash",
            unique=True,
            postgresql_where=(source_path.isnot(None)),
            sqlite_where=(source_path.isnot(None)),
        ),
        Index(
            "ix_events_session_branch_uuid",
            "session_id",
            "branch_id",
            "event_uuid",
            unique=True,
            postgresql_where=(event_uuid.isnot(None)),
            sqlite_where=(event_uuid.isnot(None)),
        ),
        Index(
            "ix_events_provisional_key",
            "session_id",
            "provisional_key",
            unique=True,
            postgresql_where=(provisional_key.isnot(None)),
            sqlite_where=(provisional_key.isnot(None)),
        ),
        Index("ix_events_session_timestamp", "session_id", "timestamp"),
        Index("ix_events_session_branch_timestamp", "session_id", "branch_id", "timestamp"),
        Index("ix_events_role_tool", "role", "tool_name"),
        Index(
            "ix_events_orphan_tool_call_scan",
            "id",
            "session_id",
            "branch_id",
            "tool_call_id",
            sqlite_where=text("event_origin = 'durable' AND role = 'assistant' AND tool_name IS NOT NULL AND tool_call_id IS NOT NULL"),
        ),
        Index(
            "ix_events_tool_result_pair",
            "session_id",
            "branch_id",
            "tool_call_id",
            sqlite_where=text("event_origin = 'durable' AND role = 'tool' AND tool_call_id IS NOT NULL"),
        ),
    )


class AgentSourceLine(AgentsBase):
    """Lossless source-line archive for a session log.

    Stores every parsed source line with byte offset so logs can be exported
    byte-for-byte even when parser schema extraction changes over time.
    """

    __tablename__ = "source_lines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    _fk_ref = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    session_id = Column(
        GUID(),
        ForeignKey(_fk_ref, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Session identity kernel — kept nullable for now: legacy ingest paths
    # write observations before the kernel thread is materialized.
    thread_id = Column(GUID(), nullable=True, index=True)
    source_path = Column(Text, nullable=False)
    source_offset = Column(BigInteger, nullable=False)
    branch_id = Column(Integer, nullable=False, index=True)
    revision = Column(Integer, nullable=False, server_default=text("1"))
    is_branch_copy = Column(Integer, nullable=False, server_default=text("0"))  # 1 when copied during rewind fork
    # codec=0: plain TEXT in raw_json; codec=1: zstd BLOB in raw_json_z (raw_json is '' sentinel)
    raw_json = Column(Text, nullable=False)
    raw_json_z = Column(LargeBinary, nullable=True)
    raw_json_codec = Column(Integer, nullable=False, server_default=text("0"))
    line_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("AgentSession", back_populates="source_lines")

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "branch_id",
            "source_path",
            "source_offset",
            "revision",
            name="uq_source_line_revision",
        ),
        UniqueConstraint(
            "session_id",
            "branch_id",
            "source_path",
            "source_offset",
            "line_hash",
            name="uq_source_line_hash",
        ),
        Index("ix_source_lines_session_offset", "session_id", "branch_id", "source_offset"),
    )


class MediaObject(AgentsBase):
    """Content-addressed media blob discovered in session archives.

    The filesystem owns bytes; this table records the integrity contract and
    the relative path needed to fetch the blob without embedding it in source
    lines, events, or ingest payloads.
    """

    __tablename__ = "media_objects"

    sha256 = Column(String(64), primary_key=True)
    mime_type = Column(String(64), nullable=False)
    byte_size = Column(BigInteger, nullable=False)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    storage_path = Column(Text, nullable=False)
    thumbnail_sha256 = Column(String(64), nullable=True, index=True)
    first_seen_session_id = Column(GUID(), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SessionMediaRef(AgentsBase):
    """Where a media object appeared in a provider session archive."""

    __tablename__ = "session_media_refs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(GUID(), nullable=False, index=True)
    event_id = Column(Integer, nullable=True, index=True)
    source_path = Column(Text, nullable=True)
    source_offset = Column(BigInteger, nullable=True)
    source_line_hash = Column(String(64), nullable=True, index=True)
    json_pointer = Column(Text, nullable=True)
    provider = Column(String(50), nullable=True, index=True)
    original_kind = Column(String(32), nullable=False)
    media_sha256 = Column(String(64), nullable=False, index=True)
    media_state = Column(String(32), nullable=False, server_default=text("'pending'"), index=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "source_path",
            "source_offset",
            "media_sha256",
            name="uq_session_media_source_ref",
        ),
        Index("ix_session_media_refs_session_state", "session_id", "media_state", "created_at"),
    )


class ArchiveChunk(AgentsBase):
    """Sealed raw archive chunk manifest row.

    The filesystem/object store owns bytes; this table records the append-only
    sealed chunk metadata that projectors and exporters can checkpoint against.
    ``session_id`` is intentionally not a cascading FK: archive metadata should
    survive hot session-row churn until an explicit decommission step.
    """

    __tablename__ = "archive_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(255), nullable=True, index=True)
    session_id = Column(GUID(), nullable=False, index=True)
    stream = Column(String(64), nullable=False, index=True)
    relative_path = Column(Text, nullable=False, unique=True)
    first_source_seq = Column(BigInteger, nullable=False)
    last_source_seq = Column(BigInteger, nullable=False)
    record_count = Column(Integer, nullable=False)
    uncompressed_bytes = Column(BigInteger, nullable=False)
    compressed_bytes = Column(BigInteger, nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    file_sha256 = Column(String(64), nullable=False)
    state = Column(String(32), nullable=False, server_default=text("'sealed'"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    sealed_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_archive_chunks_session_stream_seq", "session_id", "stream", "first_source_seq"),
        Index("ix_archive_chunks_state_created", "state", "created_at"),
    )


class TimelineCard(AgentsBase):
    """Small session-card read model for hot list/timeline paths."""

    __tablename__ = "timeline_cards"

    session_id = Column(GUID(), primary_key=True)
    provider = Column(String(50), nullable=False, index=True)
    environment = Column(String(20), nullable=False, index=True)
    project = Column(String(255), nullable=True, index=True)
    device_id = Column(String(255), nullable=True, index=True)
    cwd = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True, index=True)
    summary_title = Column(String(255), nullable=True)
    first_user_message_preview = Column(Text, nullable=True)
    last_visible_text_preview = Column(Text, nullable=True)
    last_user_message_preview = Column(Text, nullable=True)
    last_assistant_message_preview = Column(Text, nullable=True)
    user_messages = Column(Integer, nullable=False, server_default=text("0"))
    assistant_messages = Column(Integer, nullable=False, server_default=text("0"))
    tool_calls = Column(Integer, nullable=False, server_default=text("0"))
    transcript_revision = Column(Integer, nullable=False, server_default=text("0"))
    archive_state = Column(String(32), nullable=False, server_default=text("'current'"))
    archive_lag_records = Column(Integer, nullable=False, server_default=text("0"))
    archive_last_source_offset = Column(BigInteger, nullable=True)
    origin_kind = Column(String(64), nullable=True, index=True)
    hidden_from_default_timeline = Column(Integer, nullable=False, server_default=text("0"))
    launch_actor = Column(String(32), nullable=True, index=True)
    launch_surface = Column(String(32), nullable=True, index=True)
    derived_state = Column(String(32), nullable=False, server_default=text("'unknown'"))
    derived_revision = Column(String(128), nullable=True)
    parser_revision = Column(String(128), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_timeline_cards_activity", "last_activity_at", "started_at"),
        Index("ix_timeline_cards_project_provider", "project", "provider"),
    )


class ArchiveExportCheckpoint(AgentsBase):
    """Resumable legacy-export checkpoint for raw archive migration."""

    __tablename__ = "archive_export_checkpoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    exporter_name = Column(String(128), nullable=False)
    tenant_id = Column(String(255), nullable=True)
    source_table = Column(String(64), nullable=False)
    session_id = Column(GUID(), nullable=True, index=True)
    last_rowid = Column(BigInteger, nullable=False, server_default=text("0"))
    last_source_seq = Column(BigInteger, nullable=False, server_default=text("0"))
    status = Column(String(32), nullable=False, server_default=text("'idle'"))
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "exporter_name",
            "tenant_id",
            "source_table",
            "session_id",
            name="uq_archive_export_checkpoint_scope",
        ),
        Index("ix_archive_export_checkpoints_status_updated", "status", "updated_at"),
    )


class ArchiveExportQuarantine(AgentsBase):
    """Corrupt legacy raw row recorded during archive export."""

    __tablename__ = "archive_export_quarantine"

    id = Column(Integer, primary_key=True, autoincrement=True)
    exporter_name = Column(String(128), nullable=False)
    tenant_id = Column(String(255), nullable=True)
    source_table = Column(String(64), nullable=False)
    rowid = Column(BigInteger, nullable=False)
    session_id = Column(GUID(), nullable=True, index=True)
    error = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "exporter_name",
            "tenant_id",
            "source_table",
            "rowid",
            name="uq_archive_export_quarantine_row",
        ),
        Index("ix_archive_export_quarantine_session", "session_id", "source_table"),
    )


class ProjectorCheckpoint(AgentsBase):
    """Per-projector checkpoint into sealed archive chunks."""

    __tablename__ = "projector_checkpoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    projector_name = Column(String(128), nullable=False)
    parser_revision = Column(String(128), nullable=False)
    session_id = Column(GUID(), nullable=False, index=True)
    chunk_id = Column(Integer, nullable=True, index=True)
    chunk_payload_sha256 = Column(String(64), nullable=True)
    last_record_ordinal = Column(Integer, nullable=False, server_default=text("0"))
    status = Column(String(32), nullable=False, server_default=text("'idle'"))
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "projector_name",
            "parser_revision",
            "session_id",
            "chunk_id",
            name="uq_projector_checkpoint_position",
        ),
        Index("ix_projector_checkpoints_status_updated", "status", "updated_at"),
    )


class SessionObservation(AgentsBase):
    """Append-only raw observation bus for session-related facts.

    Reducers materialize transcript, archive, runtime, and timeline read models
    from these observations. The deterministic ``observation_id`` is the first
    idempotency boundary.
    """

    __tablename__ = "session_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    observation_id = Column(String(512), nullable=False)
    session_id = Column(GUID(), nullable=True, index=True)
    # Session identity kernel — kept nullable for now: legacy ingest paths
    # write observations before the kernel thread is materialized.
    thread_id = Column(GUID(), nullable=True, index=True)
    runtime_key = Column(String(255), nullable=True, index=True)
    provider = Column(String(64), nullable=False)
    device_id = Column(String(255), nullable=True)
    source_domain = Column(String(32), nullable=False, index=True)
    source = Column(String(128), nullable=False, index=True)
    kind = Column(String(64), nullable=False, index=True)
    source_path = Column(Text, nullable=True)
    source_offset = Column(BigInteger, nullable=True)
    source_cursor = Column(String(512), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payload_json = Column(Text, nullable=True)
    payload_json_z = Column(LargeBinary, nullable=True)
    payload_json_codec = Column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        UniqueConstraint("observation_id", name="uq_session_observations_observation_id"),
        Index("ix_session_observations_session_observed", "session_id", "observed_at", "id"),
        Index("ix_session_observations_session_source_kind", "session_id", "source", "kind", "id"),
        Index(
            "ix_session_observations_session_source_kind_observed",
            "session_id",
            "source",
            "kind",
            "observed_at",
            "id",
        ),
        Index("ix_session_observations_domain_kind", "source_domain", "kind", "observed_at"),
        Index("ix_session_observations_source_cursor", "source", "source_cursor"),
    )


class SessionLivePreview(AgentsBase):
    """Compact read model for live transcript preview rendering."""

    __tablename__ = "session_live_previews"

    session_id = Column(
        GUID(),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    thread_id = Column(String(255), nullable=True)
    turn_key = Column(String(512), nullable=False)
    seq = Column(Integer, nullable=True)
    preview_text = Column(Text, nullable=False)
    provisional_cursor = Column(String(512), nullable=True)
    provisional_complete = Column(Integer, nullable=False, server_default=text("0"))
    event_origin = Column(String(32), nullable=False, server_default=text("'live_provisional'"))
    preview_observed_at = Column(DateTime(timezone=True), nullable=False)
    preview_updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    source = Column(String(128), nullable=False)
    last_observation_id = Column(String(512), nullable=False)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by_event_id = Column(Integer, nullable=True)
    superseded_reason = Column(String(64), nullable=True)

    session = relationship("AgentSession", back_populates="live_preview")

    __table_args__ = (
        Index("ix_session_live_previews_updated", "preview_updated_at"),
        Index("ix_session_live_previews_observation", "last_observation_id"),
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
    last_ship_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_ship_result = Column(String(64), nullable=True)
    last_ship_latency_ms = Column(Integer, nullable=True)
    last_ship_http_status = Column(Integer, nullable=True)

    # Stats
    spool_pending = Column(Integer, default=0)
    spool_dead = Column(Integer, default=0)
    parse_errors_1h = Column(Integer, default=0)
    consecutive_failures = Column(Integer, default=0)
    ship_attempts_1h = Column(Integer, default=0)
    ship_successes_1h = Column(Integer, default=0)
    ship_rate_limited_1h = Column(Integer, default=0)
    ship_server_errors_1h = Column(Integer, default=0)
    ship_payload_rejections_1h = Column(Integer, default=0)
    ship_payload_too_large_1h = Column(Integer, default=0)
    ship_retryable_client_errors_1h = Column(Integer, default=0)
    ship_connect_errors_1h = Column(Integer, default=0)
    ship_latency_p50_ms_1h = Column(Integer, nullable=True)
    ship_latency_p95_ms_1h = Column(Integer, nullable=True)
    disk_free_bytes = Column(BigInteger, default=0)
    is_offline = Column(Integer, default=0)  # 0/1 (SQLite has no bool)

    # Full payload for forward compatibility
    raw_json = Column(Text, nullable=True)
    sessions_digest = Column(String(128), nullable=True)
    sessions_sequence = Column(Integer, nullable=True)

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


class SessionTurn(AgentsBase):
    """Canonical per-turn timing record for managed and reconstructed sessions."""

    __tablename__ = "session_turns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    _fk_ref = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    session_id = Column(
        GUID(),
        ForeignKey(_fk_ref, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Session identity kernel — kept nullable for now: legacy ingest paths
    # create SessionTurn rows before the kernel thread/run is materialized.
    thread_id = Column(GUID(), nullable=True, index=True)
    run_id = Column(GUID(), nullable=True, index=True)
    request_id = Column(String(64), nullable=True, index=True)
    session_input_id = Column(Integer, nullable=True, index=True)
    source_kind = Column(
        String(32),
        nullable=False,
        default="managed_live",
        server_default=text("'managed_live'"),
    )
    timing_confidence = Column(
        String(20),
        nullable=False,
        default="exact",
        server_default=text("'exact'"),
    )
    expected_user_text_hash = Column(String(64), nullable=True)
    state = Column(String(20), nullable=False)
    terminal_phase = Column(String(32), nullable=True)
    error_code = Column(String(64), nullable=True)
    user_event_id = Column(Integer, nullable=True)
    durable_assistant_event_id = Column(Integer, nullable=True)
    baseline_event_id = Column(Integer, nullable=True)
    baseline_observation_cursor = Column(Integer, nullable=True)
    user_submitted_at = Column(DateTime(timezone=True), nullable=False)
    send_accepted_at = Column(DateTime(timezone=True), nullable=True)
    active_phase_observed_at = Column(DateTime(timezone=True), nullable=True)
    terminal_at = Column(DateTime(timezone=True), nullable=True)
    durable_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index(
            "ix_session_turns_session_request",
            "session_id",
            "request_id",
            unique=True,
            postgresql_where=text("request_id IS NOT NULL"),
            sqlite_where=text("request_id IS NOT NULL"),
        ),
        Index("ix_session_turns_session_order", "session_id", "user_submitted_at", "created_at", "id"),
        Index("ix_session_turns_session_state_created", "session_id", "state", "created_at"),
    )


class SessionRuntimeState(AgentsBase):
    """Reducer-owned runtime projection for a session/runtime key."""

    __tablename__ = "session_runtime_state"

    runtime_key = Column(String(255), primary_key=True)
    session_id = Column(GUID(), nullable=True, index=True)
    # Session identity kernel — kept nullable for now: legacy ingest paths
    # create runtime-state rows before the kernel thread/run is materialized.
    thread_id = Column(GUID(), nullable=True, index=True)
    run_id = Column(GUID(), nullable=True, index=True)
    provider = Column(String(64), nullable=False)
    device_id = Column(String(255), nullable=True)
    phase = Column(String(32), nullable=False)
    phase_source = Column(String(32), nullable=False)
    active_tool = Column(String(128), nullable=True)
    phase_started_at = Column(DateTime(timezone=True), nullable=True)
    execution_started_at = Column(DateTime(timezone=True), nullable=True)
    last_runtime_signal_at = Column(DateTime(timezone=True), nullable=True)
    last_progress_at = Column(DateTime(timezone=True), nullable=True)
    last_live_at = Column(DateTime(timezone=True), nullable=True)
    timeline_anchor_at = Column(DateTime(timezone=True), nullable=False, index=True)
    freshness_expires_at = Column(DateTime(timezone=True), nullable=True)
    terminal_state = Column(String(32), nullable=True)
    terminal_reason = Column(String(64), nullable=True)
    terminal_source = Column(String(64), nullable=True)
    terminal_at = Column(DateTime(timezone=True), nullable=True)
    runtime_version = Column(Integer, nullable=False, server_default=text("0"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_runtime_state_session_updated_version", "session_id", "updated_at", "runtime_version"),
        Index("ix_runtime_state_anchor", "timeline_anchor_at"),
        Index("ix_runtime_state_updated", "updated_at"),
        Index("ix_runtime_state_device_provider", "device_id", "provider"),
    )


class SessionPauseRequest(AgentsBase):
    """Durable provider question waiting for a user answer.

    Phase truth stays in ``SessionRuntimeState``. This row only stores the
    actionable structured-question request that can make ``needs_user`` require
    attention.
    """

    __tablename__ = "session_pause_requests"

    id = Column(GUID(), primary_key=True, default=uuid4)
    session_id = Column(
        GUID(),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_key = Column(String(255), nullable=False, index=True)
    provider = Column(String(64), nullable=False)
    request_key = Column(String(512), nullable=False, unique=True)
    provider_request_id = Column(String(255), nullable=True)
    provider_ref_json = Column(JSON(), nullable=True)
    kind = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'pending'"))
    tool_name = Column(String(128), nullable=True)
    title = Column(String(255), nullable=True)
    summary = Column(Text, nullable=True)
    request_payload_json = Column(JSON(), nullable=True)
    response_payload_json = Column(JSON(), nullable=True)
    response_text = Column(Text, nullable=True)
    can_respond = Column(Boolean, nullable=False, default=False, server_default="false")
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_pause_requests_session_status_occurred", "session_id", "status", "occurred_at"),
        Index("ix_pause_requests_runtime_status_occurred", "runtime_key", "status", "occurred_at"),
        Index(
            "ix_pause_requests_provider_request",
            "provider",
            "provider_request_id",
            postgresql_where=text("provider_request_id IS NOT NULL"),
            sqlite_where=text("provider_request_id IS NOT NULL"),
        ),
    )


class SessionTask(AgentsBase):
    """Legacy durable task rows retained for old tenant databases.

    Summary and embedding enrichment are now driven by session revision lag,
    not this table. Keep the model so existing rows remain readable until a
    future cleanup migration removes them.
    task_type: legacy string
    status:    'pending' | 'running' | 'done' | 'failed'
    """

    __tablename__ = "session_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id = Column(String(36), nullable=False)
    task_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, nullable=False, server_default=text("5"))
    retry_later_count = Column(Integer, nullable=False, server_default=text("0"))
    # Legacy resurrection counter from the removed ingest task worker.
    resurrection_count = Column(Integer, nullable=False, server_default=text("0"))
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
        # Legacy lookup indexes retained for existing tenant DBs.
        Index("ix_session_tasks_status_created", "status", "created_at"),
        Index("ix_session_tasks_session_type_status", "session_id", "task_type", "status"),
    )


class SessionMessage(AgentsBase):
    """Durable directed message between sessions with delivery state."""

    __tablename__ = "session_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_session_id = Column(GUID(), nullable=False, index=True)
    to_session_id = Column(GUID(), nullable=False, index=True)
    body = Column("text", Text, nullable=False)
    source_event_id = Column(Integer, nullable=True)
    delivery_status = Column(String(32), nullable=False, server_default=text("'queued'"))
    delivery_attempts = Column(Integer, nullable=False, server_default=text("0"))
    last_error = Column(Text, nullable=True)
    delivered_via = Column(String(32), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_session_messages_to_status_created", "to_session_id", "delivery_status", "created_at"),
        Index("ix_session_messages_from_created", "from_session_id", "created_at"),
    )


class SessionInput(AgentsBase):
    """Durable user-originated input for a managed session.

    Separate from SessionMessage (agent-to-agent). Holds queued drafts that
    drain at the next safe turn boundary, and records the effective delivery
    mode (turn_start, steer, or queued) chosen by the dispatch path.
    """

    __tablename__ = "session_inputs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(GUID(), nullable=False, index=True)
    # Session identity kernel — kept nullable for now: legacy paths write
    # session_inputs before the kernel thread is materialized.
    thread_id = Column(GUID(), nullable=True, index=True)
    body = Column("text", Text, nullable=False)
    owner_id = Column(Integer, nullable=True, index=True)  # authoring user, null on legacy rows
    intent = Column(String(16), nullable=False)  # auto | queue | steer
    status = Column(String(16), nullable=False, server_default=text("'queued'"))
    # queued | delivering | delivered | cancelled | failed
    client_request_id = Column(String(64), nullable=True)
    delivery_request_id = Column(String(64), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_attempt_id = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_session_inputs_session_status_created", "session_id", "status", "created_at"),
        Index("ix_session_inputs_session_status_next_attempt", "session_id", "status", "next_attempt_at", "created_at"),
        Index(
            "ix_session_inputs_session_owner_client_request",
            "session_id",
            "owner_id",
            "client_request_id",
            unique=True,
            postgresql_where=text("client_request_id IS NOT NULL"),
            sqlite_where=text("client_request_id IS NOT NULL"),
        ),
    )


class SessionInputDeliveryAttempt(AgentsBase):
    """Durable delivery attempt for a managed session input.

    Phase 1 keeps this as an internal ledger. Later phases will make the
    unexpired attempt lease the cross-process authority for provider injection.
    """

    __tablename__ = "session_input_delivery_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    _input_fk = "session_inputs.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_inputs.id"
    session_input_id = Column(
        Integer,
        ForeignKey(_input_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(GUID(), nullable=False, index=True)
    thread_id = Column(GUID(), nullable=True, index=True)
    owner_id = Column(Integer, nullable=True, index=True)
    request_id = Column(String(64), nullable=False)
    attempt_number = Column(Integer, nullable=False, server_default=text("1"))
    status = Column(String(24), nullable=False)
    lease_owner = Column(String(128), nullable=False)
    lease_expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_input_attempts_input_created", "session_input_id", "created_at"),
        Index("ix_input_attempts_session_status_lease", "session_id", "status", "lease_expires_at"),
        Index(
            "ix_input_attempts_request",
            "session_id",
            "request_id",
            unique=True,
        ),
    )


class SessionInputAttachment(AgentsBase):
    """Image attachment associated with a SessionInput row.

    The blob lives on disk under ``data/attachments/<session_id>/<id>.bin``;
    this row records the metadata needed to render thumbnails, verify the
    bytes the engine fetched, and clean up after delivery.
    """

    __tablename__ = "session_input_attachments"

    id = Column(GUID(), primary_key=True, default=uuid4)
    _input_fk = "session_inputs.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_inputs.id"
    session_input_id = Column(
        Integer,
        ForeignKey(_input_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(GUID(), nullable=False, index=True)
    mime_type = Column(String(64), nullable=False)
    byte_size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    blob_path = Column(Text, nullable=False)
    original_filename = Column(String(255), nullable=True)
    original_byte_size = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


# ---------------------------------------------------------------------------
# Session identity kernel — see docs/specs/session-identity-kernel.md
#
# Seven new tables that split AgentSession's overloaded responsibilities into:
#   - Thread:        Longhouse-owned causal continuity (survives quit/resume)
#   - ThreadAlias:   provider/source identity evidence
#   - Edge:          provider-neutral relationship evidence between graph nodes
#   - Run:           one provider CLI process invocation
#   - Connection:    Longhouse's relationship to a run (control plane + state)
#   - LaunchAttempt: pre-process launch lifecycle for remote launches
#
# Phase 1 is purely additive. AgentSession child tables also gain nullable
# thread_id / run_id columns to start the migration off session_id parentage;
# those become NOT NULL in Phase 3.
# ---------------------------------------------------------------------------


class SessionThread(AgentsBase):
    """Longhouse-owned causal continuity for a session's conversation lineage.

    A thread is the unit that survives provider quit/resume. One session has
    one primary thread today; subagents and future continuations attach as
    child threads under the same session.

    Identity is the Longhouse UUID. Provider-side ids live in ``thread_aliases``
    as evidence, not identity.
    """

    __tablename__ = "session_threads"

    id = Column(GUID(), primary_key=True, default=uuid4)
    _session_fk = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    session_id = Column(
        GUID(),
        ForeignKey(_session_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(64), nullable=False, index=True)

    # Lineage — null for root threads; set for subagents and continuations.
    _parent_thread_fk = "session_threads.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_threads.id"
    parent_thread_id = Column(
        GUID(),
        ForeignKey(_parent_thread_fk, ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Replaces AgentSession.branched_from_event_id; nullable.
    parent_event_id = Column(Integer, nullable=True)
    branch_kind = Column(
        String(20),
        nullable=False,
        server_default=text("'root'"),
    )  # root | subagent | continuation | fork
    # Durable Longhouse launch-origin label for the thread. This is independent
    # from branch_kind so Hatch automation can remain a root provider transcript
    # while staying hidden from default top-level product surfaces.
    origin_kind = Column(String(64), nullable=True, index=True)
    hidden_from_default_timeline = Column(Integer, nullable=False, server_default=text("0"))

    # Denormalized "is this the session's primary thread" — matches
    # sessions.primary_thread_id. Defaults 0 so subagent/continuation threads
    # created without an explicit override never silently become a second
    # primary. Backfill and root-thread creation set this to 1 explicitly.
    is_primary = Column(Integer, nullable=False, server_default=text("0"))

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # One primary thread per session, enforced by the DB.
        Index(
            "ux_threads_one_primary_per_session",
            "session_id",
            unique=True,
            postgresql_where=text("is_primary = 1"),
            sqlite_where=text("is_primary = 1"),
        ),
        Index("ix_threads_session_primary", "session_id", "is_primary"),
        Index("ix_threads_parent", "parent_thread_id"),
    )


class SessionThreadAlias(AgentsBase):
    """Provider/source identity evidence for a thread.

    Aliases are provider/source evidence used by ingest and adoption to resolve
    which thread a new observation belongs to. Provider session ids are routing
    identity within a provider; compatibility/source aliases remain evidence.
    """

    __tablename__ = "session_thread_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    _thread_fk = "session_threads.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_threads.id"
    thread_id = Column(
        GUID(),
        ForeignKey(_thread_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(64), nullable=False)
    alias_kind = Column(
        String(48),
        nullable=False,
        index=True,
    )
    # provider_session_id | longhouse_session_id | source_path |
    # parent_provider_session_id | forked_from_provider_session_id
    alias_value = Column(String(1024), nullable=False)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_thread_aliases_lookup", "provider", "alias_kind", "alias_value"),
        Index("ix_thread_aliases_thread_kind", "thread_id", "alias_kind"),
        # A provider's native session id is the stable routing key. If it points
        # at two Longhouse threads, managed control can become read-only.
        Index(
            "ux_thread_aliases_provider_session_routing",
            "provider",
            "alias_value",
            unique=True,
            postgresql_where=text("alias_kind = 'provider_session_id'"),
            sqlite_where=text("alias_kind = 'provider_session_id'"),
        ),
        # A given thread shouldn't accumulate exact-duplicate alias rows.
        # Globally, non-provider-session aliases may still be shared.
        Index(
            "ux_thread_aliases_unique_per_thread",
            "thread_id",
            "provider",
            "alias_kind",
            "alias_value",
            unique=True,
        ),
    )


class SessionEdge(AgentsBase):
    """Provider-neutral relationship evidence between session graph nodes.

    Edges are semantic evidence. Aliases remain compatibility lookup aids, but
    product projection should prefer these rows once the read paths are flipped.
    """

    __tablename__ = "session_edges"

    id = Column(GUID(), primary_key=True, default=uuid4)
    provider = Column(String(64), nullable=False, index=True)
    edge_kind = Column(String(32), nullable=False, index=True)
    visibility = Column(String(32), nullable=False, server_default=text("'timeline'"))
    evidence_kind = Column(String(32), nullable=True)

    _session_fk = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    _thread_fk = "session_threads.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_threads.id"

    source_session_id = Column(GUID(), ForeignKey(_session_fk, ondelete="CASCADE"), nullable=True, index=True)
    source_thread_id = Column(GUID(), ForeignKey(_thread_fk, ondelete="CASCADE"), nullable=True, index=True)
    source_event_id = Column(Integer, nullable=True, index=True)

    target_session_id = Column(GUID(), ForeignKey(_session_fk, ondelete="CASCADE"), nullable=True, index=True)
    target_thread_id = Column(GUID(), ForeignKey(_thread_fk, ondelete="CASCADE"), nullable=True, index=True)
    target_event_id = Column(Integer, nullable=True, index=True)

    provider_edge_id = Column(String(255), nullable=True, index=True)
    metadata_json = Column(JSON(), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_session_edges_source", "provider", "source_session_id", "source_thread_id", "edge_kind"),
        Index("ix_session_edges_target", "provider", "target_session_id", "target_thread_id", "edge_kind"),
        Index("ix_session_edges_provider_edge", "provider", "edge_kind", "provider_edge_id"),
    )


class SessionRun(AgentsBase):
    """One provider CLI process invocation lifetime.

    Records pid, host, cwd, started/ended, exit status. Restarting a laptop
    and resuming the same thread creates a new run; it does not create a new
    session or thread.

    `boot_id` distinguishes pid reuse across reboots. `process_start_time`
    distinguishes within a single boot.
    """

    __tablename__ = "session_runs"

    id = Column(GUID(), primary_key=True, default=uuid4)
    _thread_fk = "session_threads.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_threads.id"
    thread_id = Column(
        GUID(),
        ForeignKey(_thread_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(64), nullable=False)

    # Where the process runs. host_id is the runner/machine identity that
    # also routes commands (replaces AgentSession.source_runner_id).
    host_id = Column(String(255), nullable=True, index=True)
    boot_id = Column(String(64), nullable=True)
    pid = Column(Integer, nullable=True)
    process_start_time = Column(DateTime(timezone=True), nullable=True)
    cwd = Column(Text, nullable=True)
    argv_redacted_json = Column(JSON(), nullable=True)

    launch_origin = Column(
        String(32),
        nullable=False,
        server_default=text("'longhouse_spawned'"),
    )  # longhouse_spawned | longhouse_continued | external_adopted

    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at = Column(DateTime(timezone=True), nullable=True)
    exit_status = Column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_runs_thread_started", "thread_id", "started_at"),
        Index("ix_runs_host_pid_start", "host_id", "pid", "process_start_time"),
    )


class SessionConnection(AgentsBase):
    """Longhouse's relationship to a run.

    A connection is the control attachment, not the run. Bridge dying
    mid-turn flips ``state``; it does not change the run, thread, or session.
    Multiple connections may exist for one run (e.g. log_tail observe + later
    bridge attach); the capability projection picks the best one.
    """

    __tablename__ = "session_connections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    _run_fk = "session_runs.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_runs.id"
    run_id = Column(
        GUID(),
        ForeignKey(_run_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    control_plane = Column(
        String(32),
        nullable=False,
    )  # codex_bridge | pty | runner | log_tail | none
    acquisition_kind = Column(
        String(32),
        nullable=False,
    )  # spawned_control | adopted_control | observe_only
    state = Column(
        String(32),
        nullable=False,
        server_default=text("'attached'"),
    )  # attached | degraded | detached | released | ended

    # Optional human-friendly label for attach/debug paths (replaces
    # AgentSession.managed_session_name).
    external_name = Column(String(255), nullable=True)
    device_id = Column(String(255), nullable=True, index=True)

    # Typed capability gates. Small enumerated set; queryable.
    can_send_input = Column(Integer, nullable=False, server_default=text("0"))
    can_interrupt = Column(Integer, nullable=False, server_default=text("0"))
    can_terminate = Column(Integer, nullable=False, server_default=text("0"))
    can_tail_output = Column(Integer, nullable=False, server_default=text("0"))
    can_resume = Column(Integer, nullable=False, server_default=text("0"))
    capabilities_extra_json = Column(JSON(), nullable=True)

    acquired_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    released_at = Column(DateTime(timezone=True), nullable=True)
    last_health_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_connections_run_state", "run_id", "state"),
        Index("ix_connections_state_health", "state", "last_health_at"),
        Index("ix_connections_device_state_health", "device_id", "state", "last_health_at"),
        # One control attachment per (run, control_plane). Capability projection
        # depends on this — a single run cannot have two competing pty/bridge
        # connections for the same plane.
        Index(
            "ux_connections_run_plane",
            "run_id",
            "control_plane",
            unique=True,
        ),
    )


class SessionLaunchAttempt(AgentsBase):
    """Pre-process launch lifecycle for remote/managed launches.

    Attempts can exist before any run does (the user clicked "launch" but
    dispatch is still pending). Replaces AgentSession.launch_* columns.
    Idempotency is keyed by (session_id, client_request_id). ``owner_id`` is
    duplicated here so caller request-id replay cannot cross user boundaries.
    """

    __tablename__ = "session_launch_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    _session_fk = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    _thread_fk_la = "session_threads.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_threads.id"
    _run_fk_la = "session_runs.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_runs.id"
    session_id = Column(
        GUID(),
        ForeignKey(_session_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    thread_id = Column(GUID(), ForeignKey(_thread_fk_la, ondelete="SET NULL"), nullable=True)
    run_id = Column(GUID(), ForeignKey(_run_fk_la, ondelete="SET NULL"), nullable=True)

    provider = Column(String(64), nullable=False)
    host_id = Column(String(255), nullable=True, index=True)
    owner_id = Column(Integer, nullable=True, index=True)
    execution_lifetime = Column(
        String(32),
        nullable=False,
        server_default=text("'live_control'"),
    )  # live_control | one_shot

    # Caller-provided idempotency key + dispatch correlation.
    client_request_id = Column(String(64), nullable=True, index=True)
    command_id = Column(String(64), nullable=True, index=True)

    state = Column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )  # pending | dispatched | failed | adopted | abandoned
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_launch_attempts_session_client_request",
            "session_id",
            "client_request_id",
            unique=True,
            postgresql_where=text("client_request_id IS NOT NULL"),
            sqlite_where=text("client_request_id IS NOT NULL"),
        ),
        Index("ix_launch_attempts_state_created", "state", "created_at"),
    )


class MachineControlOperation(AgentsBase):
    """Durable lifecycle for Machine Agent control work that outlives one request."""

    __tablename__ = "machine_control_operations"

    id = Column(String(36), primary_key=True)

    owner_id = Column(Integer, nullable=True, index=True)
    device_id = Column(String(255), nullable=False, index=True)
    command_type = Column(String(64), nullable=False, index=True)
    command_id = Column(String(96), nullable=False, index=True)
    provider = Column(String(64), nullable=True, index=True)

    status = Column(String(32), nullable=False, server_default=text("'queued'"))
    request_json = Column(JSON, nullable=False)
    result_json = Column(JSON, nullable=True)
    error_json = Column(JSON, nullable=True)
    timeout_secs = Column(Integer, nullable=False, server_default=text("120"))

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_machine_control_ops_owner_status", "owner_id", "status", "created_at"),
        Index("ix_machine_control_ops_command", "command_id", unique=True),
        Index(
            "ux_machine_control_provider_live_active",
            "owner_id",
            "device_id",
            "provider",
            "command_type",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running') AND provider IS NOT NULL AND command_type = 'provider.live_proof'"),
            postgresql_where=text("status IN ('queued', 'running') AND provider IS NOT NULL AND command_type = 'provider.live_proof'"),
        ),
    )
