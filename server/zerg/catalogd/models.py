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
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import text

CatalogBase = declarative_base()


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
    )


class SessionTombstone(CatalogBase):
    """Deletion fence checked by every storage-v2 manifest commit."""

    __tablename__ = "session_tombstones"

    session_id = Column(String(36), primary_key=True)
    deletion_revision = Column(BigInteger, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(64), nullable=True)
    commit_seq = Column(BigInteger, nullable=False)


__all__ = ["CatalogBase", "RawObject", "SessionTombstone", "SourceEpoch"]
