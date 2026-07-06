"""Live Store models for the hot SQLite lane.

These tables use an independent declarative base on purpose. Importing the
archive ``Base`` here would make ``initialize_live_database`` create the full
archive schema inside the hot DB, which would turn the split into ceremony.
"""

from sqlalchemy import BigInteger
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
