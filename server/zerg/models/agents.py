"""Agent session models for cross-provider session tracking.

These models store sessions from AI coding assistants (Claude Code, Codex,
Gemini, Cursor) in a provider-agnostic format.

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
    provider = Column(String(50), nullable=False, index=True)  # claude, codex, gemini, cursor

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
    event_uuid = Column(String(255), nullable=True, index=True)  # Raw line uuid (Claude/Codex/Gemini event id)
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
