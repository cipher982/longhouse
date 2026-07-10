"""Live Store models for the hot SQLite lane.

These tables use an independent declarative base on purpose. Importing the
archive ``Base`` here would make ``initialize_live_database`` create the full
archive schema inside the hot DB, which would turn the split into ceremony.
"""

from sqlalchemy import JSON
from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
from sqlalchemy.sql import text
from sqlalchemy.sql import text as sql_text

from zerg.models.types import GUID

LiveBase = declarative_base()


class LiveUser(LiveBase):
    """Bounded account identity required while the cold store is unavailable."""

    # Keep the physical name/shape compatible with the existing ``User`` ORM
    # mapping. A live-catalog Session can therefore reuse mature auth strategy
    # code without teaching it a second account model.
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    provider = Column(String(64), nullable=True)
    provider_user_id = Column(String(255), nullable=True, index=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    cp_user_id = Column(Integer, nullable=True, index=True)
    email_verified = Column(Boolean, nullable=False, server_default=text("1"))
    is_active = Column(Boolean, nullable=False, server_default=text("1"))
    role = Column(String(32), nullable=False, server_default=text("'USER'"))
    display_name = Column(String(255), nullable=True)
    avatar_url = Column(Text, nullable=True)
    prefs = Column(JSON, nullable=True)
    context = Column(JSON, nullable=False, server_default=text("'{}'"))
    last_login = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)


class LiveRefreshSession(LiveBase):
    """Browser refresh-token lineage owned by the live authentication lane."""

    __tablename__ = "refresh_sessions"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    family_id = Column(String(36), nullable=False, index=True)
    parent_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    absolute_expires_at = Column(DateTime(timezone=True), nullable=False)
    idle_expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class LiveDeviceToken(LiveBase):
    """Machine credential required for ingest, launch, and control."""

    __tablename__ = "device_tokens"

    id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, nullable=False, index=True)
    device_id = Column(String(255), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_device_tokens_owner_device", "owner_id", "device_id"),)


class LiveNotificationClientPresence(LiveBase):
    """Ephemeral browser visibility used by notification suppression."""

    __tablename__ = "notification_client_presence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, nullable=False, index=True)
    client_id = Column(String, nullable=False)
    client_type = Column(String, nullable=False, server_default=text("'web'"))
    visible = Column(Boolean, nullable=False, server_default=text("0"))
    route = Column(String, nullable=True)
    session_id = Column(String, nullable=True, index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("owner_id", "client_id", name="uq_notification_client_presence_owner_client"),
        Index("ix_notification_client_presence_owner_seen", "owner_id", "last_seen_at"),
        Index("ix_notification_client_presence_owner_visible_seen", "owner_id", "visible", "last_seen_at"),
    )


class LiveMachinePresence(LiveBase):
    """Physical compatibility table for coarse Machine Agent presence."""

    __tablename__ = "machine_presence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, nullable=False, index=True)
    device_id = Column(String(255), nullable=False)
    state = Column(String(32), nullable=False)
    source = Column(String(64), nullable=False, server_default=text("'unknown'"))
    idle_seconds = Column(Integer, nullable=True)
    measured_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("owner_id", "device_id", name="uq_live_machine_presence_owner_device"),
        Index("ix_live_machine_presence_owner_received", "owner_id", "received_at"),
    )


class LiveAPNSDeviceRegistration(LiveBase):
    __tablename__ = "apns_device_registrations"

    id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, nullable=False, index=True)
    platform = Column(String(32), nullable=False, server_default=text("'ios'"))
    device_token = Column(String(255), nullable=False)
    push_environment = Column(String(32), nullable=False, server_default=text("'sandbox'"))
    app_build_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "device_token", name="uq_apns_registration_owner_token"),
        Index("ix_apns_registration_owner_platform", "owner_id", "platform"),
    )


class LiveAPNSLiveActivityRegistration(LiveBase):
    __tablename__ = "apns_live_activity_registrations"

    id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String(64), nullable=False, index=True)
    activity_id = Column(String(255), nullable=False)
    push_token = Column(String(255), nullable=False)
    push_environment = Column(String(32), nullable=False, server_default=text("'sandbox'"))
    app_build_id = Column(String(255), nullable=True)
    last_state_hash = Column(String(64), nullable=True)
    last_push_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "activity_id", name="uq_apns_live_activity_owner_activity"),
        UniqueConstraint("owner_id", "push_token", name="uq_apns_live_activity_owner_token"),
        Index("ix_apns_live_activity_owner_session", "owner_id", "session_id", "ended_at"),
    )


class LiveAPNSWidgetPushState(LiveBase):
    __tablename__ = "apns_widget_push_states"

    owner_id = Column(Integer, primary_key=True)
    state_hash = Column(String(64), nullable=True)
    last_push_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class LiveSessionCatalog(LiveBase):
    """Bounded session identity/card fields; never contains transcript bodies."""

    __tablename__ = "live_session_catalog"

    session_id = Column(String(36), primary_key=True)
    provider = Column(String(50), nullable=False, index=True)
    environment = Column(String(20), nullable=False, index=True)
    project = Column(String(255), nullable=True, index=True)
    device_id = Column(String(255), nullable=True, index=True)
    device_name = Column(String(255), nullable=True)
    cwd = Column(Text, nullable=True)
    git_repo = Column(String(500), nullable=True)
    git_branch = Column(String(255), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True, index=True)
    user_messages = Column(Integer, nullable=False, server_default=text("0"))
    assistant_messages = Column(Integer, nullable=False, server_default=text("0"))
    tool_calls = Column(Integer, nullable=False, server_default=text("0"))
    summary = Column(Text, nullable=True)
    summary_title = Column(String(255), nullable=True)
    anchor_title = Column(String(255), nullable=True)
    first_user_message_preview = Column(Text, nullable=True)
    last_visible_text_preview = Column(Text, nullable=True)
    last_user_message_preview = Column(Text, nullable=True)
    last_assistant_message_preview = Column(Text, nullable=True)
    transcript_revision = Column(Integer, nullable=False, server_default=text("0"))
    summary_revision = Column(Integer, nullable=False, server_default=text("0"))
    user_state = Column(String(20), nullable=False, server_default=text("'active'"), index=True)
    user_state_at = Column(DateTime(timezone=True), nullable=True)
    primary_thread_id = Column(String(36), nullable=True, index=True)
    loop_mode = Column(String(32), nullable=False, server_default=text("'assist'"))
    notification_muted = Column(Boolean, nullable=False, server_default=text("0"))
    origin_kind = Column(String(64), nullable=True, index=True)
    hidden_from_default_timeline = Column(Integer, nullable=False, server_default=text("0"))
    launch_actor = Column(String(32), nullable=True, index=True)
    launch_surface = Column(String(32), nullable=True, index=True)
    permission_mode = Column(String(32), nullable=False, server_default=text("'bypass'"))
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_live_session_catalog_project_started", "project", "started_at"),
        Index("ix_live_session_catalog_provider_started", "provider", "started_at"),
    )


class LiveTimelineCard(LiveBase):
    """Materialized list projection for timeline reads without the cold store."""

    __tablename__ = "live_timeline_cards"

    session_id = Column(String(36), primary_key=True)
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
    updated_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_live_timeline_cards_activity", "last_activity_at", "started_at"),
        Index("ix_live_timeline_cards_project_provider", "project", "provider"),
    )


class LiveSessionThread(LiveBase):
    __tablename__ = "live_session_threads"

    id = Column(String(36), primary_key=True)
    session_id = Column(String(36), nullable=False, index=True)
    provider = Column(String(64), nullable=False, index=True)
    parent_thread_id = Column(String(36), nullable=True, index=True)
    parent_event_id = Column(Integer, nullable=True)
    branch_kind = Column(String(20), nullable=False, server_default=text("'root'"))
    origin_kind = Column(String(64), nullable=True, index=True)
    hidden_from_default_timeline = Column(Integer, nullable=False, server_default=text("0"))
    is_primary = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class LiveSessionThreadAlias(LiveBase):
    __tablename__ = "live_session_thread_aliases"

    id = Column(Integer, primary_key=True)
    thread_id = Column(String(36), nullable=False, index=True)
    provider = Column(String(64), nullable=False)
    alias_kind = Column(String(48), nullable=False, index=True)
    alias_value = Column(String(1024), nullable=False)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_live_thread_aliases_lookup", "provider", "alias_kind", "alias_value"),
        Index("ix_live_thread_aliases_thread_kind", "thread_id", "alias_kind"),
        UniqueConstraint("thread_id", "provider", "alias_kind", "alias_value", name="uq_live_thread_alias"),
    )


class LiveSessionRun(LiveBase):
    __tablename__ = "live_session_runs"

    id = Column(String(36), primary_key=True)
    thread_id = Column(String(36), nullable=False, index=True)
    provider = Column(String(64), nullable=False)
    host_id = Column(String(255), nullable=True, index=True)
    boot_id = Column(String(64), nullable=True)
    pid = Column(Integer, nullable=True)
    process_start_time = Column(DateTime(timezone=True), nullable=True)
    cwd = Column(Text, nullable=True)
    argv_redacted_json = Column(Text, nullable=True)
    launch_origin = Column(String(32), nullable=False, server_default=text("'longhouse_spawned'"))
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    exit_status = Column(String(64), nullable=True)


class LiveSessionConnection(LiveBase):
    __tablename__ = "live_session_connections"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(36), nullable=False, index=True)
    control_plane = Column(String(32), nullable=False)
    acquisition_kind = Column(String(32), nullable=False)
    state = Column(String(32), nullable=False, server_default=text("'attached'"), index=True)
    external_name = Column(String(255), nullable=True)
    device_id = Column(String(255), nullable=True, index=True)
    can_send_input = Column(Integer, nullable=False, server_default=text("0"))
    can_interrupt = Column(Integer, nullable=False, server_default=text("0"))
    can_terminate = Column(Integer, nullable=False, server_default=text("0"))
    can_tail_output = Column(Integer, nullable=False, server_default=text("0"))
    can_resume = Column(Integer, nullable=False, server_default=text("0"))
    capabilities_extra_json = Column(Text, nullable=True)
    acquired_at = Column(DateTime(timezone=True), nullable=False)
    released_at = Column(DateTime(timezone=True), nullable=True)
    last_health_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("run_id", "control_plane", name="uq_live_connection_run_plane"),)


class LiveSessionLaunchAttempt(LiveBase):
    __tablename__ = "live_session_launch_attempts"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(36), nullable=False, index=True)
    thread_id = Column(String(36), nullable=True)
    run_id = Column(String(36), nullable=True)
    provider = Column(String(64), nullable=False)
    host_id = Column(String(255), nullable=True, index=True)
    owner_id = Column(Integer, nullable=True, index=True)
    execution_lifetime = Column(String(32), nullable=False, server_default=text("'live_control'"))
    client_request_id = Column(String(64), nullable=True, index=True)
    command_id = Column(String(64), nullable=True, index=True)
    state = Column(String(32), nullable=False, server_default=text("'pending'"), index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_live_launch_attempts_session_client_request",
            "session_id",
            "client_request_id",
            unique=True,
            sqlite_where=sql_text("client_request_id IS NOT NULL"),
        ),
    )


class LiveSession(LiveBase):
    __tablename__ = "live_sessions"

    session_id = Column(String(36), primary_key=True)
    owner_id = Column(String(36), nullable=True, index=True)
    provider = Column(String(50), nullable=False, index=True)
    device_id = Column(String(255), nullable=True, index=True)
    machine_id = Column(String(255), nullable=True)
    state = Column(String(32), nullable=False, server_default="unknown")
    started_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class LiveRuntimeState(LiveBase):
    __tablename__ = "live_runtime_state"

    runtime_key = Column(String(255), primary_key=True)
    session_id = Column(GUID(), nullable=True, index=True)
    thread_id = Column(GUID(), nullable=True, index=True)
    run_id = Column(GUID(), nullable=True, index=True)
    provider = Column(String(64), nullable=False, index=True)
    device_id = Column(String(255), nullable=True, index=True)
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
        Index("ix_live_runtime_state_session_updated_version", "session_id", "updated_at", "runtime_version"),
        Index("ix_live_runtime_state_anchor", "timeline_anchor_at"),
        Index("ix_live_runtime_state_updated", "updated_at"),
        Index("ix_live_runtime_state_device_provider", "device_id", "provider"),
    )


class LiveControlLease(LiveBase):
    __tablename__ = "live_control_leases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), nullable=False)
    provider = Column(String(50), nullable=False, index=True)
    device_id = Column(String(255), nullable=False, index=True)
    machine_id = Column(String(255), nullable=True)
    state = Column(String(32), nullable=False)
    sequence = Column(Integer, nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=False, index=True)
    payload_json = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("session_id", "provider", "device_id", name="uq_live_control_lease"),)


class LiveLaunchReadiness(LiveBase):
    __tablename__ = "live_launch_readiness"

    session_id = Column(String(36), primary_key=True)
    owner_id = Column(String(36), nullable=True, index=True)
    client_request_id = Column(String(255), nullable=True)
    provider = Column(String(64), nullable=False, index=True)
    device_id = Column(String(255), nullable=False, index=True)
    machine_id = Column(String(255), nullable=True)
    project = Column(String(255), nullable=True)
    execution_lifetime = Column(String(32), nullable=False)
    state = Column(String(32), nullable=False, server_default="pending", index=True)
    command_id = Column(String(96), nullable=True, index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "device_id",
            "provider",
            "client_request_id",
            name="uq_live_launch_readiness_client_request",
        ),
        Index("ix_live_launch_readiness_state_expires", "state", "expires_at"),
    )


class LiveSessionLivePreview(LiveBase):
    """Compact live-lane preview for in-flight transcript rendering."""

    __tablename__ = "live_session_live_previews"

    session_id = Column(String(36), primary_key=True)
    thread_id = Column(String(255), nullable=True)
    turn_key = Column(String(512), nullable=False)
    seq = Column(Integer, nullable=True)
    preview_text = Column(Text, nullable=False)
    provisional_cursor = Column(String(512), nullable=True)
    provisional_complete = Column(Integer, nullable=False, server_default=text("0"))
    event_origin = Column(String(32), nullable=False, server_default=text("'live_provisional'"))
    preview_observed_at = Column(DateTime(timezone=True), nullable=False)
    preview_updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    source = Column(String(128), nullable=False)
    last_observation_id = Column(String(512), nullable=False)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by_event_id = Column(Integer, nullable=True)
    superseded_reason = Column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_live_session_live_previews_updated", "preview_updated_at"),
        Index("ix_live_session_live_previews_observation", "last_observation_id"),
    )


class LiveMachineControlOperation(LiveBase):
    """Hot-lane lifecycle for machine-control work that outlives one request."""

    __tablename__ = "live_machine_control_operations"

    id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, nullable=True, index=True)
    session_id = Column(String(36), nullable=True, index=True)
    device_id = Column(String(255), nullable=False, index=True)
    command_type = Column(String(64), nullable=False, index=True)
    command_id = Column(String(96), nullable=False, index=True)
    provider = Column(String(64), nullable=True, index=True)
    status = Column(String(32), nullable=False, server_default=text("'queued'"))
    request_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=True)
    error_json = Column(Text, nullable=True)
    timeout_secs = Column(Integer, nullable=False, server_default=text("120"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_live_machine_control_ops_owner_status", "owner_id", "status", "created_at"),
        Index("ix_live_machine_control_ops_command", "command_id", unique=True),
        Index(
            "ux_live_machine_control_provider_live_active",
            "owner_id",
            "device_id",
            "provider",
            "command_type",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running') AND provider IS NOT NULL AND command_type = 'provider.live_proof'"),
        ),
    )


class LiveSessionInputReceipt(LiveBase):
    """Hot-lane user text input receipt before archive projection is required."""

    __tablename__ = "live_session_input_receipts"

    id = Column(String(36), primary_key=True)
    owner_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String(36), nullable=False, index=True)
    thread_id = Column(String(36), nullable=True, index=True)
    provider = Column(String(64), nullable=False, index=True)
    device_id = Column(String(255), nullable=True, index=True)
    client_request_id = Column(String(255), nullable=True)
    intent = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, index=True)
    text = Column(Text, nullable=False)
    archive_session_input_id = Column(Integer, nullable=True, index=True)
    control_command_id = Column(String(96), nullable=True, index=True)
    delivery_request_id = Column(String(64), nullable=True, index=True)
    error_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index(
            "ux_live_session_input_receipts_client_request",
            "owner_id",
            "session_id",
            "client_request_id",
            unique=True,
            sqlite_where=sql_text("client_request_id IS NOT NULL"),
        ),
        Index("ix_live_session_input_receipts_session_status_created", "session_id", "status", "created_at"),
    )


class LiveArchiveOutbox(LiveBase):
    __tablename__ = "live_archive_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(512), nullable=False, unique=True)
    kind = Column(String(64), nullable=False, index=True)
    payload_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    drained_at = Column(DateTime(timezone=True), nullable=True, index=True)
    attempts = Column(Integer, nullable=False, server_default="0")
    last_error = Column(Text, nullable=True)

    __table_args__ = (Index("ix_live_archive_outbox_drain", "drained_at", "created_at"),)


class LiveHeartbeatStamp(LiveBase):
    __tablename__ = "live_heartbeat_stamps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String(255), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    version = Column(String(50), nullable=True)
    last_ship_at = Column(DateTime(timezone=True), nullable=True)
    last_ship_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_ship_result = Column(String(64), nullable=True)
    last_ship_latency_ms = Column(Integer, nullable=True)
    last_ship_http_status = Column(Integer, nullable=True)
    spool_pending = Column(Integer, nullable=False, server_default="0")
    spool_dead = Column(Integer, nullable=False, server_default="0")
    parse_errors_1h = Column(Integer, nullable=False, server_default="0")
    consecutive_failures = Column(Integer, nullable=False, server_default="0")
    ship_attempts_1h = Column(Integer, nullable=False, server_default="0")
    ship_successes_1h = Column(Integer, nullable=False, server_default="0")
    ship_rate_limited_1h = Column(Integer, nullable=False, server_default="0")
    ship_server_errors_1h = Column(Integer, nullable=False, server_default="0")
    ship_payload_rejections_1h = Column(Integer, nullable=False, server_default="0")
    ship_payload_too_large_1h = Column(Integer, nullable=False, server_default="0")
    ship_retryable_client_errors_1h = Column(Integer, nullable=False, server_default="0")
    ship_connect_errors_1h = Column(Integer, nullable=False, server_default="0")
    ship_latency_p50_ms_1h = Column(Integer, nullable=True)
    ship_latency_p95_ms_1h = Column(Integer, nullable=True)
    disk_free_bytes = Column(BigInteger, nullable=False, server_default="0")
    is_offline = Column(Integer, nullable=False, server_default="0")
    raw_json = Column(Text, nullable=True)
    sessions_digest = Column(String(128), nullable=True)
    sessions_sequence = Column(Integer, nullable=True)

    __table_args__ = (Index("ix_live_heartbeats_device_received", "device_id", "received_at"),)
