"""Agent session models for cross-provider session tracking.

These models store sessions from AI coding assistants (Claude Code, Codex,
Antigravity, legacy Gemini, Cursor) in a provider-agnostic format.

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
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from zerg.database import Base
from zerg.models.types import GUID
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_loop_mode import SessionLoopMode

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

    # Primary key - UUID allows federation and prevents collision
    # GUID TypeDecorator: UUID for Postgres, CHAR(36) for SQLite
    id = Column(GUID(), primary_key=True, default=uuid4)

    # Provider identification
    provider = Column(String(50), nullable=False, index=True)  # claude, codex, antigravity, gemini, cursor

    # Environment classification (required - no default, caller must specify)
    environment = Column(String(20), nullable=False, index=True)  # production, development, test, e2e

    # Context
    project = Column(String(255), nullable=True, index=True)  # Project name (parsed from cwd)
    device_id = Column(String(255), nullable=True, index=True)  # Machine identifier
    device_name = Column(String(255), nullable=True)  # Human-friendly device label (e.g. "laptop", "cube")
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

    # Provider-specific session ID (e.g., Claude Code session UUID from filename)
    provider_session_id = Column(String(255), nullable=True, index=True)

    # Product-level continuation lineage (distinct from rewind/source-line branches).
    thread_root_session_id = Column(GUID(), nullable=True, index=True)
    continued_from_session_id = Column(GUID(), nullable=True, index=True)
    continuation_kind = Column(String(20), nullable=True)  # local, cloud, runner
    origin_label = Column(String(255), nullable=True)
    branched_from_event_id = Column(Integer, nullable=True)
    is_writable_head = Column(Integer, nullable=False, server_default=text("1"))

    # Pre-computed summary (generated async after ingest)
    summary = Column(Text, nullable=True)  # 2-4 sentence quick summary
    summary_title = Column(String(200), nullable=True)  # Short title for startup continuity and summaries
    # Events covered by current summary (legacy count-based cursor)
    summary_event_count = Column(Integer, server_default=text("0"))
    # ID of last AgentEvent included in summary (efficient cursor)
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

    # User-driven bucket classification (set via POST /sessions/{id}/action)
    # active (default) | parked | snoozed | archived
    user_state = Column(String(20), nullable=False, server_default=text("'active'"))
    user_state_at = Column(DateTime(timezone=True), nullable=True)
    execution_home = Column(
        String(32),
        nullable=False,
        server_default=text(f"'{SessionExecutionHome.UNMANAGED_LOCAL.value}'"),
        index=True,
    )
    managed_transport = Column(String(32), nullable=True)
    source_runner_id = Column(Integer, nullable=True, index=True)
    source_runner_name = Column(String(255), nullable=True)
    managed_session_name = Column(String(255), nullable=True)
    loop_mode = Column(String(20), nullable=False, server_default=text(f"'{SessionLoopMode.ASSIST.value}'"))
    # legacy — loop controller removed, column kept for DB compat
    loop_thread_id = Column(Integer, nullable=True, index=True)

    # Debounce outgoing mobile pager pushes when a session flaps in and out of
    # attention states.
    last_attention_push_at = Column(DateTime(timezone=True), nullable=True)
    last_attention_push_state = Column(String(20), nullable=True)

    # Sidechain flag: True when session is a Task sub-agent (not a human-initiated session)
    is_sidechain = Column(Integer, nullable=False, server_default=text("0"))

    # Session identity kernel — primary thread pointer. Nullable during
    # Phase 1/2 because backfill creates the thread row after the session.
    # See docs/specs/session-identity-kernel.md.
    primary_thread_id = Column(GUID(), nullable=True, index=True)

    # Remote-launch lifecycle (see docs/specs/remote-session-launch.md).
    # NULL means "launched the old way"; treat as equivalent to 'live'.
    launch_state = Column(String(32), nullable=True)
    launch_error_code = Column(String(64), nullable=True)
    launch_error_message = Column(Text, nullable=True)
    launch_lease_until = Column(DateTime(timezone=True), nullable=True)
    launch_command_id = Column(String(64), nullable=True, index=True)
    launch_client_request_id = Column(String(64), nullable=True, index=True)

    # Relationships
    branches = relationship("AgentSessionBranch", back_populates="session", cascade="all, delete-orphan")
    events = relationship("AgentEvent", back_populates="session", cascade="all, delete-orphan")
    source_lines = relationship("AgentSourceLine", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_sessions_project_started", "project", "started_at"),
        Index("ix_sessions_provider_started", "provider", "started_at"),
        Index("ix_sessions_thread_head", "thread_root_session_id", "is_writable_head"),
        Index("ix_sessions_continued_from_started", "continued_from_session_id", "started_at"),
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

    # Foreign key to session - GUID TypeDecorator handles UUID/String conversion
    # ForeignKey reference is dynamic based on schema (None for SQLite, "agents" for Postgres)
    _fk_ref = "sessions.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.sessions.id"
    session_id = Column(
        GUID(),
        ForeignKey(_fk_ref, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Session identity kernel — nullable in Phase 1, NOT NULL in Phase 3.
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
    event_uuid = Column(String(255), nullable=True, index=True)  # Raw line uuid (Claude/Codex/Antigravity/Gemini event id)
    parent_event_uuid = Column(String(255), nullable=True, index=True)  # Raw parent linkage id (Claude parentUuid)
    event_origin = Column(String(32), nullable=False, server_default=text("'durable'"), index=True)
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
    # Session identity kernel — nullable in Phase 1, NOT NULL in Phase 3.
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
    # Session identity kernel — observations carry thread_id when known so
    # subagent observations don't collide with the parent session's stream.
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
    # Session identity kernel — nullable in Phase 1, NOT NULL in Phase 3.
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
    # Session identity kernel — runtime state moves to run-keyed parentage in
    # Phase 3 so a stale run cannot pollute a resumed run. Nullable in Phase 1.
    thread_id = Column(GUID(), nullable=True, index=True)
    run_id = Column(GUID(), nullable=True, index=True)
    provider = Column(String(64), nullable=False)
    device_id = Column(String(255), nullable=True)
    phase = Column(String(32), nullable=False)
    phase_source = Column(String(32), nullable=False)
    active_tool = Column(String(128), nullable=True)
    phase_started_at = Column(DateTime(timezone=True), nullable=True)
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


class ManagedSessionControlState(AgentsBase):
    """Reducer-owned control-liveness projection for a managed session.

    This is intentionally separate from ``SessionRuntimeState``. Runtime state
    answers what the provider is doing; this row answers whether Longhouse has
    a fresh managed control path for the session.
    """

    __tablename__ = "managed_session_control_state"

    session_id = Column(GUID(), primary_key=True)
    provider = Column(String(64), nullable=False)
    device_id = Column(String(255), nullable=True, index=True)
    machine_id = Column(String(255), nullable=True)
    transport = Column(String(64), nullable=True)
    lease_state = Column(String(32), nullable=False, server_default=text("'unknown'"))
    control_state = Column(String(32), nullable=False, server_default=text("'unknown'"))
    reason = Column(String(64), nullable=True)
    source = Column(String(64), nullable=False, server_default=text("'machine_heartbeat'"))
    sequence = Column(Integer, nullable=True)
    last_control_seen_at = Column(DateTime(timezone=True), nullable=True)
    lease_observed_at = Column(DateTime(timezone=True), nullable=True)
    lease_ttl_ms = Column(Integer, nullable=True)
    control_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    bridge_status = Column(String(64), nullable=True)
    thread_subscription_status = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_managed_control_device_state", "device_id", "control_state"),
        Index("ix_managed_control_expires", "control_expires_at"),
    )


class UnmanagedSessionBinding(AgentsBase):
    """Machine-agent observed binding of an unmanaged provider CLI process to
    its JSONL transcript.

    Phase 5 of docs/specs/session-liveness-honesty.md. Populated by the
    Rust engine's heartbeat. Lets the Runtime Host verify whether an
    unmanaged session's underlying process is still alive so Phase 6 can
    honestly promote lifecycle=closed on confirmed process death.

    Identity is (machine_id, provider, provider_session_id). When the
    provider_session_id is unstable or absent, (machine_id, provider,
    source_inode, source_device) is the fallback identity.

    Liveness is (pid, process_start_time) — pid alone is not trusted
    because of reuse. A change in process_start_time for the same pid
    closes the previous binding as stale.

    Auto-created via AgentsBase.metadata.create_all() — no Alembic required.
    """

    __tablename__ = "unmanaged_session_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    machine_id = Column(String(255), nullable=False, index=True)
    device_id = Column(String(255), nullable=True)
    provider = Column(String(64), nullable=False)
    provider_session_id = Column(String(255), nullable=False)
    session_id = Column(GUID(), nullable=True, index=True)
    source_path = Column(String(1024), nullable=True)
    source_inode = Column(Integer, nullable=True)
    source_device = Column(Integer, nullable=True)
    pid = Column(Integer, nullable=True)
    process_start_time = Column(DateTime(timezone=True), nullable=True)
    cwd = Column(String(1024), nullable=True)
    # Latest JSONL progress the agent saw for this session
    source_offset = Column(Integer, nullable=True)
    source_mtime = Column(DateTime(timezone=True), nullable=True)
    # Liveness bookkeeping
    observed_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # 'observed': pid+start_time confirm alive; 'missing': process not in latest
    # scan but still within stale window; 'stale': confirmed gone / superseded.
    binding_state = Column(String(32), nullable=False, server_default=text("'observed'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "machine_id",
            "provider",
            "provider_session_id",
            name="uq_unmanaged_binding_identity",
        ),
        Index("ix_unmanaged_binding_session", "session_id"),
        Index("ix_unmanaged_binding_last_seen", "last_seen_at"),
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
    # Session identity kernel — nullable in Phase 1, NOT NULL in Phase 3.
    thread_id = Column(GUID(), nullable=True, index=True)
    body = Column("text", Text, nullable=False)
    owner_id = Column(Integer, nullable=True, index=True)  # authoring user, null on legacy rows
    intent = Column(String(16), nullable=False)  # auto | queue | steer
    status = Column(String(16), nullable=False, server_default=text("'queued'"))
    # queued | delivering | delivered | cancelled | failed
    client_request_id = Column(String(64), nullable=True)
    delivery_request_id = Column(String(64), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_session_inputs_session_status_created", "session_id", "status", "created_at"),
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
# Six new tables that split AgentSession's overloaded responsibilities into:
#   - Thread:        Longhouse-owned causal continuity (survives quit/resume)
#   - ThreadAlias:   provider/source identity evidence
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
    _parent_thread_fk = (
        "session_threads.id" if AGENTS_SCHEMA is None else f"{AGENTS_SCHEMA}.session_threads.id"
    )
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
    )  # root | subagent | continuation

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

    Aliases are non-authoritative pointers used by ingest and adoption to
    resolve which thread a new observation belongs to. They are NOT thread
    identity. Multiple threads may share an alias value (e.g. copied
    transcripts pre-divergence); resolver rules in Phase 2/4 handle that.
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
    )  # provider_session_id | longhouse_session_id | source_path | forked_from_provider_session_id
    alias_value = Column(String(1024), nullable=False)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_thread_aliases_lookup", "provider", "alias_kind", "alias_value"),
        Index("ix_thread_aliases_thread_kind", "thread_id", "alias_kind"),
        # Aliases are evidence, not identity, but a given thread shouldn't
        # accumulate exact-duplicate alias rows. Globally the same alias may
        # legitimately appear on multiple threads (copied transcripts before
        # divergence) — this index intentionally scopes to thread.
        Index(
            "ux_thread_aliases_unique_per_thread",
            "thread_id",
            "provider",
            "alias_kind",
            "alias_value",
            unique=True,
        ),
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
    )  # longhouse_spawned | external_adopted

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
    Idempotency is keyed by (session_id, client_request_id).
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
