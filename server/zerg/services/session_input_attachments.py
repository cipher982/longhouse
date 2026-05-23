"""Lifecycle for image attachments hung off SessionInput rows.

Blobs live on disk; metadata lives in ``session_input_attachments``. The
engine fetches blobs by id over a machine-token endpoint so we never load
the bytes through Python on the dispatch path.

The model is deliberately narrow: we trust the client's compression and
the row's sha256 is the integrity contract. Nothing here decodes images.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Iterable
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_FAILED

logger = logging.getLogger(__name__)


ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024  # 2 MB hard server limit
MAX_ATTACHMENTS_PER_INPUT = 4

# Blobs older than this whose parent session_input is in a terminal state
# get reaped on the hourly cleanup pass.
BLOB_RETENTION_SECS = 24 * 3600


@dataclass(frozen=True)
class StoredAttachment:
    id: UUID
    session_input_id: int
    session_id: UUID
    mime_type: str
    byte_size: int
    sha256: str
    blob_path: Path
    original_filename: str | None
    original_byte_size: int | None


def attachment_blob_root() -> Path:
    """Return the on-disk root for attachment blobs.

    Honors ``LONGHOUSE_ATTACHMENT_BLOB_ROOT`` for tests; otherwise lives
    under the standard ``data/`` directory alongside the SQLite db.
    """
    override = os.getenv("LONGHOUSE_ATTACHMENT_BLOB_ROOT")
    if override:
        return Path(override)
    return get_settings().data_dir / "attachments"


def _blob_path_for(session_id: UUID, attach_id: UUID) -> Path:
    return attachment_blob_root() / str(session_id) / f"{attach_id}.bin"


def store_attachment_blob(
    db: Session,
    *,
    session_input: SessionInput,
    mime_type: str,
    data: bytes,
    original_filename: str | None,
    original_byte_size: int | None,
) -> StoredAttachment:
    """Persist a single attachment blob to disk and record its row.

    Caller is responsible for caps (count + size); this is the
    inner write. The function commits the row so the blob path always
    has a corresponding metadata record on disk.
    """
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"unsupported mime: {mime_type}")
    if len(data) == 0:
        raise ValueError("empty attachment")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes")

    attach_id = uuid4()
    blob_path = _blob_path_for(session_input.session_id, attach_id)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    # Atomic write so a crash mid-write never leaves a half-blob with a row
    # claiming integrity over it.
    tmp_path = blob_path.with_suffix(".tmp")
    tmp_path.write_bytes(data)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, blob_path)

    relative = blob_path.relative_to(attachment_blob_root())
    row = SessionInputAttachment(
        id=attach_id,
        session_input_id=int(session_input.id),
        session_id=session_input.session_id,
        mime_type=mime_type,
        byte_size=len(data),
        sha256=digest,
        blob_path=str(relative),
        original_filename=original_filename,
        original_byte_size=original_byte_size,
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        # Row never landed; remove the just-written blob so disk and metadata
        # don't drift. The reaper can't see this blob — there's no row to join.
        db.rollback()
        try:
            blob_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("attachment store: failed to unlink orphan blob %s", blob_path, exc_info=True)
        raise
    db.refresh(row)
    return _to_stored(row)


def list_attachments_for_input(db: Session, session_input_id: int) -> list[StoredAttachment]:
    rows = (
        db.query(SessionInputAttachment)
        .filter(SessionInputAttachment.session_input_id == session_input_id)
        .order_by(SessionInputAttachment.created_at.asc(), SessionInputAttachment.id.asc())
        .all()
    )
    return [_to_stored(r) for r in rows]


def get_attachment(db: Session, attachment_id: UUID) -> SessionInputAttachment | None:
    return (
        db.query(SessionInputAttachment)
        .filter(SessionInputAttachment.id == attachment_id)
        .first()
    )


def absolute_blob_path(row: SessionInputAttachment) -> Path:
    return attachment_blob_root() / row.blob_path


def cleanup_stale_blobs(db: Session, *, retention_secs: int = BLOB_RETENTION_SECS) -> int:
    """Delete blobs+rows whose parent input is terminal and older than retention.

    Cascading FK already deletes the row when a session_input is hard-deleted;
    this handler covers the soft-terminal case (delivered/failed) where the
    parent row still exists but the bytes are no longer needed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention_secs)
    stale: Iterable[SessionInputAttachment] = (
        db.query(SessionInputAttachment)
        .join(SessionInput, SessionInput.id == SessionInputAttachment.session_input_id)
        .filter(
            SessionInput.status.in_((INPUT_STATUS_DELIVERED, INPUT_STATUS_FAILED)),
            SessionInputAttachment.created_at < cutoff,
        )
        .all()
    )
    removed = 0
    for row in list(stale):
        path = absolute_blob_path(row)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            logger.warning("attachment cleanup: failed to unlink %s", path, exc_info=True)
        db.delete(row)
        removed += 1
    if removed:
        db.commit()
        logger.info("attachment cleanup: removed %d stale blobs", removed)
    return removed


def _to_stored(row: SessionInputAttachment) -> StoredAttachment:
    return StoredAttachment(
        id=row.id,
        session_input_id=int(row.session_input_id),
        session_id=row.session_id,
        mime_type=row.mime_type,
        byte_size=int(row.byte_size),
        sha256=row.sha256,
        blob_path=absolute_blob_path(row),
        original_filename=row.original_filename,
        original_byte_size=int(row.original_byte_size) if row.original_byte_size is not None else None,
    )
