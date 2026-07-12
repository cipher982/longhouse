"""Catalogd-exclusive storage-v2 manifest models.

These tables deliberately do not live on ``LiveBase``: Runtime Host legacy
initialization still owns that metadata during the cutover, while only catalogd
may create or open storage-v2 catalog tables.
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
from sqlalchemy.sql import text

CatalogBase = declarative_base()


class StorageSession(CatalogBase):
    """One denormalized session row for timeline and storage convergence."""

    __tablename__ = "sessions"

    session_id = Column(String(36), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    owner_id = Column(String(64), nullable=True, index=True)
    provider = Column(String(32), nullable=False, index=True)
    environment = Column(String(32), nullable=False, server_default=text("'local'"), index=True)
    machine_id = Column(String(255), nullable=False, index=True)
    project = Column(String(255), nullable=True, index=True)
    cwd = Column(Text, nullable=True)
    git_repo = Column(String(500), nullable=True)
    git_branch = Column(String(255), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    user_messages = Column(Integer, nullable=False, server_default=text("0"))
    assistant_messages = Column(Integer, nullable=False, server_default=text("0"))
    tool_calls = Column(Integer, nullable=False, server_default=text("0"))
    summary_title = Column(String(255), nullable=True)
    first_user_message_preview = Column(Text, nullable=True)
    last_visible_text_preview = Column(Text, nullable=True)
    transcript_revision = Column(BigInteger, nullable=False, server_default=text("0"))
    current_render_generation = Column(String(36), nullable=True, index=True)
    raw_state = Column(String(16), nullable=False, server_default=text("'pending'"), index=True)
    render_state = Column(String(16), nullable=False, server_default=text("'pending'"), index=True)
    media_state = Column(String(16), nullable=False, server_default=text("'complete'"), index=True)
    missing_media_hashes_json = Column(Text, nullable=False, server_default=text("'[]'"))
    user_state = Column(String(20), nullable=False, server_default=text("'active'"), index=True)
    loop_mode = Column(String(32), nullable=False, server_default=text("'assist'"))
    notification_muted = Column(Integer, nullable=False, server_default=text("0"))
    origin_kind = Column(String(64), nullable=True, index=True)
    hidden_from_default_timeline = Column(Integer, nullable=False, server_default=text("0"))
    launch_actor = Column(String(32), nullable=True, index=True)
    launch_surface = Column(String(32), nullable=True, index=True)
    commit_seq = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        Index("ix_sessions_timeline", "user_state", "last_activity_at", "started_at", "session_id"),
        Index(
            "ix_sessions_owner_timeline",
            "owner_id",
            "hidden_from_default_timeline",
            "user_state",
            "last_activity_at",
            "session_id",
        ),
        Index("ix_sessions_project_provider", "project", "provider", "last_activity_at"),
    )


class SourceEpoch(CatalogBase):
    """Stable provider-source namespace for immutable storage-v2 ranges."""

    __tablename__ = "source_epochs"

    source_epoch = Column(String(36), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    machine_id = Column(String(255), nullable=False)
    provider = Column(String(32), nullable=False)
    opaque_source_id = Column(Text, nullable=False)
    range_kind = Column(String(32), nullable=False)
    state = Column(String(16), nullable=False, server_default=text("'open'"), index=True)
    predecessor_source_epoch = Column(String(36), nullable=True, index=True)
    replaced_by_source_epoch = Column(String(36), nullable=True, index=True)
    accepted_through = Column(String(20), nullable=False, server_default=text("'00000000000000000000'"))
    object_count = Column(Integer, nullable=False, server_default=text("0"))
    commit_seq = Column(BigInteger, nullable=False)
    closed_commit_seq = Column(BigInteger, nullable=True)
    opened_at = Column(DateTime(timezone=True), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_source_epochs_identity",
            "tenant_id",
            "machine_id",
            "provider",
            "opaque_source_id",
            "opened_at",
        ),
    )


class RawObject(CatalogBase):
    """Durable receipt and manifest row for one sealed immutable envelope."""

    __tablename__ = "raw_objects"

    envelope_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    session_id = Column(String(36), nullable=False, index=True)
    machine_id = Column(String(255), nullable=False)
    provider = Column(String(32), nullable=False)
    opaque_source_id = Column(Text, nullable=False)
    source_epoch = Column(String(36), nullable=False, index=True)
    range_kind = Column(String(32), nullable=False)
    range_start = Column(String(20), nullable=False)
    range_end = Column(String(20), nullable=False)
    record_count = Column(Integer, nullable=False)
    record_hashes_hash = Column(String(64), nullable=False)
    object_hash = Column(String(64), nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False)
    compressed_hash = Column(String(64), nullable=False)
    object_path = Column(Text, nullable=False)
    uncompressed_size = Column(BigInteger, nullable=False)
    compressed_size = Column(BigInteger, nullable=False)
    provenance_kind = Column(String(32), nullable=False, server_default=text("'native'"))
    media_refs_hash = Column(String(64), nullable=True)
    render_state = Column(String(16), nullable=False, server_default=text("'pending'"))
    media_state = Column(String(16), nullable=False, server_default=text("'complete'"))
    missing_media_hashes_json = Column(Text, nullable=False, server_default=text("'[]'"))
    commit_seq = Column(BigInteger, nullable=False)
    sealed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    retired_at = Column(DateTime(timezone=True), nullable=True, index=True)
    retirement_revision = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index(
            "ix_raw_objects_epoch_range",
            "source_epoch",
            "range_start",
            "range_end",
            unique=True,
        ),
        Index("ix_raw_objects_source_identity", "tenant_id", "machine_id", "provider", "opaque_source_id"),
        Index(
            "ix_raw_objects_session_source_order",
            "session_id",
            "machine_id",
            "provider",
            "opaque_source_id",
            "source_epoch",
            "range_start",
            "envelope_id",
        ),
    )


class RenderGeneration(CatalogBase):
    """Parser/order-qualified generation selected atomically per session."""

    __tablename__ = "render_generations"

    generation_id = Column(String(36), primary_key=True)
    session_id = Column(String(36), nullable=False, index=True)
    parser_revision = Column(String(128), nullable=False)
    ordering_revision = Column(String(128), nullable=False)
    state = Column(String(16), nullable=False, index=True)
    source_chain_hash = Column(String(64), nullable=False)
    object_count = Column(Integer, nullable=False, server_default=text("0"))
    event_count = Column(BigInteger, nullable=False, server_default=text("0"))
    first_order_key = Column(Text, nullable=True)
    last_order_key = Column(Text, nullable=True)
    commit_seq = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    superseded_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "parser_revision", "ordering_revision", name="uq_render_generation_revision"),
        Index("ix_render_generations_session_state", "session_id", "state", "updated_at"),
    )


class RenderObject(CatalogBase):
    """Manifest for one immutable bounded render object."""

    __tablename__ = "render_objects"

    object_id = Column(String(64), primary_key=True)
    generation_id = Column(String(36), nullable=False, index=True)
    session_id = Column(String(36), nullable=False, index=True)
    source_envelope_id = Column(String(64), nullable=False, index=True)
    object_hash = Column(String(64), nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False)
    object_path = Column(Text, nullable=False)
    uncompressed_size = Column(BigInteger, nullable=False)
    compressed_size = Column(BigInteger, nullable=False)
    event_count = Column(Integer, nullable=False)
    first_order_key = Column(Text, nullable=True)
    last_order_key = Column(Text, nullable=True)
    first_order_time_us = Column(BigInteger, nullable=True)
    first_machine_id = Column(String(255), nullable=True)
    first_provider = Column(String(32), nullable=True)
    first_opaque_source_id = Column(Text, nullable=True)
    first_source_epoch = Column(String(36), nullable=True)
    first_source_position = Column(BigInteger, nullable=True)
    first_event_subordinal = Column(BigInteger, nullable=True)
    last_order_time_us = Column(BigInteger, nullable=True)
    last_machine_id = Column(String(255), nullable=True)
    last_provider = Column(String(32), nullable=True)
    last_opaque_source_id = Column(Text, nullable=True)
    last_source_epoch = Column(String(36), nullable=True)
    last_source_position = Column(BigInteger, nullable=True)
    last_event_subordinal = Column(BigInteger, nullable=True)
    commit_seq = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    retired_at = Column(DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("generation_id", "source_envelope_id", name="uq_render_object_generation_source"),
        Index(
            "ix_render_objects_generation_first_order",
            "generation_id",
            "first_order_time_us",
            "first_machine_id",
            "first_provider",
            "first_opaque_source_id",
            "first_source_epoch",
            "first_source_position",
            "first_event_subordinal",
            "object_id",
        ),
        Index(
            "ix_render_objects_generation_last_order",
            "generation_id",
            "last_order_time_us",
            "last_machine_id",
            "last_provider",
            "last_opaque_source_id",
            "last_source_epoch",
            "last_source_position",
            "last_event_subordinal",
        ),
    )


class SessionTombstone(CatalogBase):
    """Deletion fence checked by every storage-v2 manifest commit."""

    __tablename__ = "session_tombstones"

    session_id = Column(String(36), primary_key=True)
    deletion_revision = Column(BigInteger, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(64), nullable=True)
    commit_seq = Column(BigInteger, nullable=False)


class MediaObject(CatalogBase):
    """Content-addressed media manifest; never stores media bytes."""

    __tablename__ = "media_objects"

    media_hash = Column(String(64), primary_key=True)
    state = Column(String(16), nullable=False, index=True)
    mime_type = Column(String(255), nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    object_path = Column(Text, nullable=True)
    commit_seq = Column(BigInteger, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class SessionMediaRef(CatalogBase):
    """Stable session-to-media membership independent of transcript parsing."""

    __tablename__ = "session_media_refs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), nullable=False, index=True)
    media_hash = Column(String(64), nullable=False, index=True)
    envelope_id = Column(String(64), nullable=True, index=True)
    ref_key = Column(String(255), nullable=False)
    state = Column(String(16), nullable=False, server_default=text("'active'"), index=True)
    commit_seq = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    deletion_revision = Column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "media_hash", "envelope_id", "ref_key", name="uq_session_media_ref"),
        Index("ix_session_media_refs_session_state", "session_id", "state", "id"),
    )


class ProjectorState(CatalogBase):
    """One coalescing desired/completed revision row per projector/session."""

    __tablename__ = "projector_state"

    projector = Column(String(64), primary_key=True)
    session_id = Column(String(36), primary_key=True)
    desired_revision = Column(BigInteger, nullable=False, server_default=text("0"))
    completed_revision = Column(BigInteger, nullable=False, server_default=text("0"))
    claimed_revision = Column(BigInteger, nullable=True)
    claim_token = Column(String(64), nullable=True, index=True)
    worker_id = Column(String(255), nullable=True)
    claim_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    status = Column(String(16), nullable=False, server_default=text("'idle'"), index=True)
    failure_count = Column(Integer, nullable=False, server_default=text("0"))
    last_error_code = Column(String(64), nullable=True)
    last_error_message = Column(Text, nullable=True)
    retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_completion_token = Column(String(64), nullable=True)
    last_failure_token = Column(String(64), nullable=True)
    commit_seq = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_projector_state_lag", "projector", "completed_revision", "desired_revision", "session_id"),)


__all__ = [
    "CatalogBase",
    "MediaObject",
    "ProjectorState",
    "RawObject",
    "RenderGeneration",
    "RenderObject",
    "SessionMediaRef",
    "SessionTombstone",
    "SourceEpoch",
    "StorageSession",
]
