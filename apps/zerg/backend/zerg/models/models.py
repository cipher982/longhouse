from sqlalchemy import JSON

# SQLAlchemy core imports
from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float
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
from zerg.models.enums import AgentStatus
from zerg.models_config import DEFAULT_WORKER_MODEL_ID
from zerg.models.enums import Phase
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.enums import ThreadType
from zerg.models.enums import UserRole

# ---------------------------------------------------------------------------
# Authentication – User table (Stage 1)
# ---------------------------------------------------------------------------


class User(Base):
    """Application user.

    For the MVP we only support Google sign-in, but we leave provider fields
    generic to allow future providers (e.g. GitHub, email, etc.).
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # OAuth provider details -------------------------------------------------
    provider = Column(String, nullable=True, default="google")
    provider_user_id = Column(String, nullable=True, index=True)

    # Core identity ----------------------------------------------------------
    email = Column(String, unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Role / permission level – backed by :class:`zerg.models.enums.UserRole`.
    role = Column(
        SAEnum(UserRole, native_enum=False, name="user_role_enum"),
        nullable=False,
        default=UserRole.USER.value,
    )

    # -------------------------------------------------------------------
    # Personalisation fields (introduced in *User Personalisation* feature)
    # -------------------------------------------------------------------
    # Optional display name shown in the UI (fallback: e-mail)
    display_name = Column(String, nullable=True)
    # User-supplied avatar URL (fallback: generated initial)
    avatar_url = Column(String, nullable=True)
    # Store arbitrary UI preferences (theme, timezone, etc.)
    prefs = Column(MutableDict.as_mutable(JSON), nullable=True, default={})

    # User context for prompt composition (servers, integrations, preferences)
    context = Column(MutableDict.as_mutable(JSON), nullable=False, default={})

    # Login tracking
    last_login = Column(DateTime, nullable=True)

    # -------------------------------------------------------------------
    # Google Mail integration (Phase-2 Email Triggers)
    # -------------------------------------------------------------------
    # When a user connects their Gmail account with *offline_access* scope we
    # receive a **refresh token** that allows the backend to fetch short-lived
    # access-tokens without further user interaction.  Persist the token
    # encrypted-at-rest in a future iteration – for now we store the raw value
    # because unit-tests run against an ephemeral in-memory SQLite database.

    gmail_refresh_token = Column(String, nullable=True)

    # -------------------------------------------------------------------
    # Convenience property used by the API layer / Pydantic models.
    # -------------------------------------------------------------------

    @property
    def gmail_connected(self) -> bool:  # noqa: D401 – simple boolean accessor
        """Return *True* if the user granted offline Gmail access (refresh token stored)."""

        return self.gmail_refresh_token is not None

    # Timestamps -------------------------------------------------------------
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# Integrations – Connectors (single source of truth for provider creds)
# ---------------------------------------------------------------------------


class Connector(Base):
    """External integration connector.

    Stores provider credentials/config for both inbound (triggers) and
    outbound (notifications/actions). This becomes the single source of
    truth – triggers reference a connector, and providers operate strictly
    via a connector id.
    """

    __tablename__ = "connectors"
    __table_args__ = (
        # Ensure a user cannot create duplicate connectors for the same
        # (type, provider) pair. Prevents accidental duplicates during
        # repeated connect flows.
        UniqueConstraint("owner_id", "type", "provider", name="uix_connector_owner_type_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Ownership – the user who created/owns this connector
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    owner = relationship("User", backref="connectors")

    # High-level type and provider identifier
    # Examples: type="email", provider="gmail" | provider="smtp"
    type = Column(String, nullable=False)
    provider = Column(String, nullable=False)

    # Provider-specific configuration (encrypted secrets, meta, watch info, etc.)
    config = Column(MutableDict.as_mutable(JSON), nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    status = Column(
        SAEnum(AgentStatus, native_enum=False, name="agent_status_enum"),
        default=AgentStatus.IDLE.value,
    )
    system_instructions = Column(Text, nullable=False)
    task_instructions = Column(Text, nullable=False)
    schedule = Column(String, nullable=True)  # CRON expression or interval
    model = Column(String, nullable=False)  # Model to use (no default)
    config = Column(MutableDict.as_mutable(JSON), nullable=True)  # Additional configuration as JSON

    # -------------------------------------------------------------------
    # Tool allowlist – controls which tools this agent can use
    # -------------------------------------------------------------------
    # Empty/NULL means all tools are allowed. Otherwise, it's a JSON array
    # of tool names that the agent is allowed to use. Supports wildcards
    # like "http_*" to allow all HTTP tools.
    allowed_tools = Column(MutableList.as_mutable(JSON), nullable=True)

    # -------------------------------------------------------------------
    # Ownership – every agent belongs to *one* user (creator / owner).
    # -------------------------------------------------------------------

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Bidirectional relationship so ``agent.owner`` returns the User row and
    # ``user.agents`` lists all agents owned by the user.
    owner = relationship("User", backref="agents")
    # Scheduling metadata
    # Next time this agent is currently expected to run.  Updated by the
    # SchedulerService whenever a cron job is (re)scheduled.
    next_run_at = Column(DateTime, nullable=True)
    # Last time a scheduled (or manual) run actually finished successfully.
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    # --------------------------------------------------
    # *run_on_schedule* has been removed – the presence of a non-NULL cron string
    # in the *schedule* column now **alone** determines whether the Scheduler
    # service will run the agent.  A NULL / empty schedule means "disabled".
    # --------------------------------------------------
    last_error = Column(Text, nullable=True)  # Store the last error message

    # Define relationship with AgentMessage
    messages = relationship("AgentMessage", back_populates="agent", cascade="all, delete-orphan")
    # Define relationship with Thread
    threads = relationship("Thread", back_populates="agent", cascade="all, delete-orphan")

    # Relationship to execution runs (added in the *Run History* feature).
    runs = relationship("AgentRun", back_populates="agent", cascade="all, delete-orphan")


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


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    role = Column(String, nullable=False)  # "system", "user", "assistant"
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, server_default=func.now())

    # Define relationship with Agent
    agent = relationship("Agent", back_populates="messages")


class Thread(Base):
    __tablename__ = "agent_threads"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"))
    title = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    # Store additional metadata like agent state
    agent_state = Column(MutableDict.as_mutable(JSON), nullable=True)
    memory_strategy = Column(String, default="buffer", nullable=True)
    thread_type = Column(
        SAEnum(ThreadType, native_enum=False, name="thread_type_enum"),
        default=ThreadType.CHAT.value,
        nullable=False,
    )  # Types: chat, scheduled, manual
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Define relationship with Agent
    agent = relationship("Agent", back_populates="threads")
    # Define relationship with ThreadMessage
    messages = relationship("ThreadMessage", back_populates="thread", cascade="all, delete-orphan")


# ------------------------------------------------------------
# Triggers
# ------------------------------------------------------------


class Trigger(Base):
    """A trigger that can fire an agent (e.g. via webhook).

    Currently only the *webhook* type is implemented.  Each trigger owns a
    unique secret token that must be supplied when the webhook is invoked.
    """

    __tablename__ = "triggers"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)

    # For now we only support webhook triggers but leave room for future
    # extension (e.g. kafka, email, slack, etc.).
    type = Column(String, default="webhook", nullable=False)

    # Shared secret that must accompany incoming webhook calls.  Very simple
    # scheme for now – a random hex string.
    secret = Column(String, nullable=False, unique=True, index=True)

    # Optional JSON blob with trigger-specific configuration.  This keeps the
    # model forward-compatible so new trigger types (e.g. *email*, *slack*)
    # can persist arbitrary settings without schema migrations.  For webhook
    # triggers the column is generally **NULL**.
    config = Column(MutableDict.as_mutable(JSON), nullable=True)

    # -------------------------------------------------------------------
    # Typed *config* accessor
    # -------------------------------------------------------------------

    @property
    def config_obj(self):  # noqa: D401 – typed accessor
        """Return a :class:`TriggerConfig` parsed from ``config`` JSON.

        No caching / fallback logic – we simply construct a new model every
        call because the cost is negligible and keeps the implementation
        straightforward.
        """

        from zerg.models.trigger_config import TriggerConfig  # local import

        return TriggerConfig(**(self.config or {}))  # type: ignore[arg-type]

    def set_config_obj(self, cfg):  # noqa: D401 – mutator, TriggerConfig param
        """Assign *cfg* and persist its dict representation."""

        # Persist as raw dict so DB schema remains unchanged
        raw = cfg.model_dump()  # type: ignore[attr-defined]

        self.config = raw  # type: ignore[assignment]

    created_at = Column(DateTime, server_default=func.now())

    # ORM relationships
    agent = relationship("Agent", backref="triggers")


class ThreadMessage(Base):
    __tablename__ = "thread_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(Integer, ForeignKey("agent_threads.id"))
    role = Column(String, nullable=False)  # "system", "user", "assistant", "tool"
    content = Column(Text, nullable=False)
    # Store *list* of tool call dicts emitted by OpenAI ChatCompleteion
    tool_calls = Column(MutableList.as_mutable(JSON), nullable=True)
    tool_call_id = Column(String, nullable=True)  # For tool responses
    name = Column(String, nullable=True)  # For tool messages
    sent_at = Column(DateTime(timezone=True), server_default=func.now())  # When user sent the message (UTC)
    processed = Column(Boolean, default=False, nullable=False)  # Track if message has been processed by agent
    message_metadata = Column(MutableDict.as_mutable(JSON), nullable=True)  # Store additional metadata
    parent_id = Column(Integer, ForeignKey("thread_messages.id"), nullable=True)

    # Define relationship with Thread
    thread = relationship("Thread", back_populates="messages")


# ---------------------------------------------------------------------------
# AgentRun – lightweight execution telemetry row
# ---------------------------------------------------------------------------


class AgentRun(Base):
    """Represents a single *execution* of an Agent.

    An AgentRun is created whenever an agent task is executed either manually,
    via the scheduler or through an external trigger.  It references the
    underlying *Thread* that captures the chat transcript but keeps
    additional execution-level metadata (status, timing, cost, etc.) that is
    cumbersome to derive from the chat model alone.
    """

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign keys -------------------------------------------------------
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    thread_id = Column(Integer, ForeignKey("agent_threads.id"), nullable=False)

    # Lifecycle ----------------------------------------------------------
    status = Column(
        SAEnum(RunStatus, native_enum=False, name="run_status_enum"),
        default=RunStatus.QUEUED.value,
        nullable=False,
    )  # queued → running → success|failed
    trigger = Column(
        SAEnum(RunTrigger, native_enum=False, name="run_trigger_enum"),
        default=RunTrigger.MANUAL.value,
        nullable=False,
    )  # manual / schedule / api

    # Timing -------------------------------------------------------------
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    # Usage --------------------------------------------------------------
    total_tokens = Column(Integer, nullable=True)
    total_cost_usd = Column(Float, nullable=True)

    # Failure ------------------------------------------------------------
    error = Column(Text, nullable=True)
    cancel_reason = Column(Text, nullable=True)

    # Summary ------------------------------------------------------------
    # Brief summary of the run for Jarvis Task Inbox (first assistant response or truncated output)
    summary = Column(Text, nullable=True)

    # Timestamps ---------------------------------------------------------
    # Note: nullable=True for SQLite compatibility with existing tables
    # New rows will have defaults, existing rows backfilled by migration
    created_at = Column(DateTime, server_default=func.now(), nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=True)

    # Relationships ------------------------------------------------------
    agent = relationship("Agent", back_populates="runs")
    thread = relationship("Thread", backref="runs")


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
    __table_args__ = (
        CheckConstraint("(phase='finished') = (result IS NOT NULL)", name="phase_result_consistency_node"),
    )

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
