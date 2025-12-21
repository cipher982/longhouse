from sqlalchemy import JSON

# SQLAlchemy core imports
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

# Local helpers / enums
from zerg.database import Base
from zerg.models.enums import Phase
from zerg.models_config import DEFAULT_WORKER_MODEL_ID

# Re-export models that have been split into separate files for backwards compatibility
from .agent import Agent  # noqa: F401
from .agent import AgentMessage  # noqa: F401
from .connector import Connector  # noqa: F401
from .run import AgentRun  # noqa: F401
from .thread import Thread  # noqa: F401
from .thread import ThreadMessage  # noqa: F401
from .trigger import Trigger  # noqa: F401
from .user import User  # noqa: F401

# ---------------------------------------------------------------------------
# Integrations – Connectors (single source of truth for provider creds)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CanvasLayout – persist per-user canvas/UI state (Phase-B)
# ---------------------------------------------------------------------------


class CanvasLayout(Base):
    """Persisted *canvas layout* for a user.

    At the moment every user stores at most **one** layout (keyed by
    ``workspace`` = NULL).  The table is future-proofed for multi-tenant
    scenarios by including an optional *workspace* column.
    """

    __tablename__ = "canvas_layouts"

    # Enforce *one layout per (user, workspace)*.  Workspace is currently
    # always ``NULL`` but the uniqueness constraint makes future multi-tenant
    # work easier and allows us to rely on an atomic *upsert* in the CRUD
    # helper.
    __table_args__ = (
        # Ensure a user has at most *one* layout per workflow.
        UniqueConstraint("user_id", "workflow_id", name="uix_user_workflow_layout"),
    )

    id = Column(Integer, primary_key=True)

    # Foreign key to *users* – **NOT NULL**.  A NULL value would break the
    # UNIQUE(user_id, workspace) constraint in SQLite because every row that
    # contains a NULL is considered *distinct*.  That would allow unlimited
    # duplicate layouts for anonymous users which is *never* what we want.
    #
    # For the dev-mode bypass (`AUTH_DISABLED`) the helper in
    # `zerg.dependencies.auth` ensures a deterministic *dev@local* user row
    # is always present so a proper `user_id` exists.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Reserved for a future multi-tenant feature where a user can switch
    # between different *workspaces*.
    workspace = Column(String, nullable=True)

    # NEW – link layout to a specific **workflow**.  NULL = global / legacy.
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=True)

    # Raw JSON blobs coming from the WASM frontend.
    nodes_json = Column(MutableDict.as_mutable(JSON), nullable=False)
    viewport = Column(MutableDict.as_mutable(JSON), nullable=True)

    # Track last update timestamp (creation time is implicit – equals first
    # value of *updated_at*).
    # Let the **database** rather than Python set and update the timestamp so
    # values are consistent across multiple application instances and not
    # subject to clock skew.
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ORM relationship back to the owning user – one-to-one convenience.
    user = relationship("User", backref="canvas_layout", uselist=False)

    # Backref to owning workflow (optional)
    workflow = relationship("Workflow", backref="canvas_layouts", uselist=False)


# ------------------------------------------------------------
# Triggers
# ------------------------------------------------------------

# ---------------------------------------------------------------------------
# AgentRun – lightweight execution telemetry row
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Workflow – visual workflow definition and persistence
# ---------------------------------------------------------------------------


class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    canvas = Column(MutableDict.as_mutable(JSON), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ORM relationship to User
    owner = relationship("User", backref="workflows")


class WorkflowTemplate(Base):
    __tablename__ = "workflow_templates"

    id = Column(Integer, primary_key=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String, nullable=False, index=True)
    canvas = Column(MutableDict.as_mutable(JSON), nullable=False)
    tags = Column(JSON, nullable=True, default=lambda: [])  # List of strings
    preview_image_url = Column(String, nullable=True)
    is_public = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ORM relationships
    creator = relationship("User", backref="created_templates")


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"

    # Add constraint for Phase/Result consistency
    __table_args__ = (CheckConstraint("(phase='finished') = (result IS NOT NULL)", name="phase_result_consistency_wf"),)

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=False, index=True)

    # Phase/Result architecture
    phase = Column(
        String,
        nullable=False,
        default=Phase.WAITING.value,
        server_default=Phase.WAITING.value,
    )
    result = Column(String, nullable=True, server_default=None)
    attempt_no = Column(Integer, nullable=False, default=1, server_default="1")
    failure_kind = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    heartbeat_ts = Column(DateTime, nullable=True)

    # Existing fields
    triggered_by = Column(String, nullable=True, default="manual")  # manual, schedule, webhook, email, etc.
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    log = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ORM relationships
    workflow = relationship("Workflow", backref="executions")
    node_states = relationship("NodeExecutionState", back_populates="workflow_execution", cascade="all, delete-orphan")


class NodeExecutionState(Base):
    __tablename__ = "node_execution_states"

    # Add constraint for Phase/Result consistency
    __table_args__ = (CheckConstraint("(phase='finished') = (result IS NOT NULL)", name="phase_result_consistency_node"),)

    id = Column(Integer, primary_key=True, index=True)
    workflow_execution_id = Column(Integer, ForeignKey("workflow_executions.id"), nullable=False, index=True)
    node_id = Column(String, nullable=False)

    # Phase/Result architecture
    phase = Column(
        String,
        nullable=False,
        default=Phase.WAITING.value,
        server_default=Phase.WAITING.value,
    )
    result = Column(String, nullable=True, server_default=None)
    attempt_no = Column(Integer, nullable=False, default=1, server_default="1")
    failure_kind = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    heartbeat_ts = Column(DateTime, nullable=True)

    # Existing fields
    output = Column(MutableDict.as_mutable(JSON), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ORM relationship
    workflow_execution = relationship("WorkflowExecution", back_populates="node_states")


# ---------------------------------------------------------------------------
# ConnectorCredential – encrypted credentials for built-in connector tools
# ---------------------------------------------------------------------------


class ConnectorCredential(Base):
    """Encrypted credential for a built-in connector tool.

    Scoped to a single agent. Each agent can have at most one credential
    per connector type (e.g., one Slack webhook, one GitHub token).

    Credentials are stored encrypted using Fernet (AES-GCM) via the
    ``zerg.utils.crypto`` module. The ``encrypted_value`` column contains
    a JSON blob with the credential fields specific to each connector type.
    """

    __tablename__ = "connector_credentials"
    __table_args__ = (
        # One credential per connector type per agent
        UniqueConstraint("agent_id", "connector_type", name="uix_agent_connector"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to agent with CASCADE delete – when an agent is deleted,
    # all its credentials are automatically removed.
    agent_id = Column(
        Integer,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Connector type identifier: 'slack', 'discord', 'email', 'sms',
    # 'github', 'jira', 'linear', 'notion', 'imessage'
    connector_type = Column(String(50), nullable=False)

    # Encrypted credential value (Fernet AES-GCM).
    # Stored as JSON containing connector-specific fields:
    # - Slack/Discord: {"webhook_url": "..."}
    # - GitHub: {"token": "..."}
    # - Jira: {"domain": "...", "email": "...", "api_token": "..."}
    encrypted_value = Column(Text, nullable=False)

    # Optional user-friendly label (e.g., "#engineering channel")
    display_name = Column(String(255), nullable=True)

    # Metadata discovered during test (e.g., GitHub username, Slack workspace).
    # Stored as JSON, NOT encrypted (no secrets here).
    # Note: Named "connector_metadata" to avoid conflict with SQLAlchemy's reserved "metadata".
    connector_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)

    # Test status tracking
    test_status = Column(String(20), nullable=False, default="untested")
    last_tested_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    agent = relationship("Agent", backref="connector_credentials")


# ---------------------------------------------------------------------------
# AccountConnectorCredential – account-level credentials for built-in tools
# ---------------------------------------------------------------------------


class AccountConnectorCredential(Base):
    """Account-level encrypted credential for built-in connector tools.

    These credentials are shared across all agents owned by the user.
    Agents can optionally override with per-agent credentials in
    ConnectorCredential (agent-level overrides).

    Resolution order in CredentialResolver:
    1. Agent-level override (ConnectorCredential)
    2. Account-level credential (this table)
    3. None if neither exists

    The organization_id column is nullable and reserved for future
    multi-tenant support. When populated, credentials can be shared
    across an organization and agents reference organization_id for
    credential resolution.
    """

    __tablename__ = "account_connector_credentials"
    __table_args__ = (
        # One credential per connector type per owner
        UniqueConstraint("owner_id", "connector_type", name="uix_account_owner_connector"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Owner – the user who owns this credential
    owner_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Reserved for future organization/team support
    # When populated, enforces (organization_id, connector_type) uniqueness
    organization_id = Column(Integer, nullable=True, index=True)

    # Connector type identifier: 'slack', 'discord', 'email', 'sms',
    # 'github', 'jira', 'linear', 'notion', 'imessage'
    connector_type = Column(String(50), nullable=False)

    # Encrypted credential value (Fernet AES-GCM).
    # Same format as ConnectorCredential.encrypted_value
    encrypted_value = Column(Text, nullable=False)

    # Optional user-friendly label (e.g., "Engineering Slack workspace")
    display_name = Column(String(255), nullable=True)

    # Metadata discovered during test (e.g., GitHub username, Slack workspace).
    # Stored as JSON, NOT encrypted (no secrets here).
    connector_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)

    # Test status tracking
    test_status = Column(String(20), nullable=False, default="untested")
    last_tested_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    owner = relationship("User", backref="account_connector_credentials")


# ---------------------------------------------------------------------------
# Worker Jobs – Background task execution for supervisor agents
# ---------------------------------------------------------------------------


class WorkerJob(Base):
    """Background job for executing worker agent tasks.

    Worker jobs allow supervisor agents to delegate long-running tasks
    to background workers without blocking the supervisor's execution flow.
    """

    __tablename__ = "worker_jobs"

    id = Column(Integer, primary_key=True, index=True)

    # Job ownership and security
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Supervisor correlation - links worker to supervisor run for SSE event streaming
    # ON DELETE SET NULL: if supervisor run is deleted, worker job remains but loses correlation
    supervisor_run_id = Column(Integer, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True)

    # Job specification
    task = Column(Text, nullable=False)
    model = Column(String(100), nullable=False, default=DEFAULT_WORKER_MODEL_ID)

    # Execution state
    status = Column(String(20), nullable=False, default="queued")  # queued, running, success, failed
    worker_id = Column(String(255), nullable=True, index=True)  # Set when execution starts

    # Error handling
    error = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    # Relationships
    owner = relationship("User", backref="worker_jobs")


# ---------------------------------------------------------------------------
# Runners – User-owned execution infrastructure (Runners v1)
# ---------------------------------------------------------------------------


class Runner(Base):
    """User-owned runner daemon for executing commands.

    Runners connect outbound to the Swarmlet platform and execute jobs
    on behalf of workers. This enables secure execution without backend
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

    worker_id = Column(String, nullable=True, index=True)  # Link to WorkerArtifactStore
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


# ---------------------------------------------------------------------------
# Knowledge Base – Sources and Documents (Phase 0)
# ---------------------------------------------------------------------------


class KnowledgeSource(Base):
    """User-owned knowledge source (URL, git repo, upload, etc.).

    Each source syncs on a schedule and produces searchable documents.
    Phase 0 only supports 'url' type.
    """

    __tablename__ = "knowledge_sources"

    id = Column(Integer, primary_key=True, index=True)

    # Ownership - every source belongs to one user
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    owner = relationship("User", backref="knowledge_sources")

    # Source type: "url" (Phase 0), "git_repo", "upload", "manual_note" (Phase 1+)
    source_type = Column(String(50), nullable=False)

    # User-friendly label
    name = Column(String(255), nullable=False)

    # Type-specific configuration (e.g., {"url": "...", "auth_header": "..."})
    config = Column(MutableDict.as_mutable(JSON), nullable=False)

    # Optional cron expression for automatic sync (e.g., "0 * * * *" for hourly)
    sync_schedule = Column(String(100), nullable=True)

    # Sync state
    last_synced_at = Column(DateTime, nullable=True)
    sync_status = Column(String(50), default="pending", nullable=False)  # pending, success, failed
    sync_error = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    documents = relationship("KnowledgeDocument", back_populates="source", cascade="all, delete-orphan")


class KnowledgeDocument(Base):
    """A single document fetched from a knowledge source.

    Stores normalized text content for searching.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        # Ensure one document per (source, path) combination
        UniqueConstraint("source_id", "path", name="uq_source_path"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to source (CASCADE delete when source is removed)
    source_id = Column(Integer, ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False, index=True)

    # Denormalized owner_id for efficient querying
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Original path/URL
    path = Column(String(1024), nullable=False)

    # Extracted or inferred title
    title = Column(String(512), nullable=True)

    # Normalized text content (searchable)
    content_text = Column(Text, nullable=False)

    # SHA-256 hash for change detection
    content_hash = Column(String(64), nullable=False)

    # Additional metadata (mime type, size, etc.)
    # Note: Named "doc_metadata" to avoid conflict with SQLAlchemy's reserved "metadata"
    doc_metadata = Column(MutableDict.as_mutable(JSON), nullable=True, default={})

    # When this document was last fetched
    fetched_at = Column(DateTime, nullable=False)

    # Relationships
    source = relationship("KnowledgeSource", back_populates="documents")
    owner = relationship("User", backref="knowledge_documents")


# ---------------------------------------------------------------------------
# User Tasks – Agent-created tasks for users
# ---------------------------------------------------------------------------


class UserTask(Base):
    """A task created by an agent for a user.

    Agents can use task management tools to create, update, and track
    tasks for their users. This provides a lightweight task management
    system without external dependencies.
    """

    __tablename__ = "user_tasks"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to user (CASCADE delete when user is removed)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Task details
    title = Column(Text, nullable=False)
    notes = Column(Text, nullable=True)

    # Status: pending, done, cancelled
    status = Column(String(20), nullable=False, default="pending")

    # Optional due date
    due_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    user = relationship("User", backref="user_tasks")


# ---------------------------------------------------------------------------
# Agent Memory – Persistent key-value storage for agents
# ---------------------------------------------------------------------------


class AgentMemoryKV(Base):
    """Persistent key-value memory storage for agents.

    Allows agents to store and retrieve arbitrary data across conversations.
    Each entry is scoped to a user and can be tagged for easy retrieval.
    Optional expiration allows for automatic cleanup of temporary data.
    """

    __tablename__ = "agent_memory_kv"

    # Composite primary key (user_id, key)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True)
    key = Column(Text, nullable=False, primary_key=True)

    # JSON value - can store any JSON-serializable data (dict, list, string, number, bool)
    # Don't use MutableDict here since the value can be any JSON type, not just dict
    value = Column(JSON, nullable=False)

    # Optional tags for filtering (stored as JSON array)
    tags = Column(MutableList.as_mutable(JSON), nullable=True, default=lambda: [])

    # Optional expiration
    expires_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    user = relationship("User", backref="agent_memory")
