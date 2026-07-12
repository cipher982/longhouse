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
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment
from zerg.services.media_store import absolute_media_path
from zerg.services.media_store import store_media_blob
from zerg.services.media_store import upsert_media_ref
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
    session_input_id: int | str
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


async def store_catalog_attachment_blob(
    *,
    input_receipt_id: str,
    owner_id: int,
    session_id: UUID,
    mime_type: str,
    data: bytes,
    original_filename: str | None,
    original_byte_size: int | None,
) -> StoredAttachment:
    """Write attachment bytes locally and persist bounded metadata via catalogd."""

    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"unsupported mime: {mime_type}")
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("attachment size is outside the supported range")
    receipt_uuid = UUID(input_receipt_id)
    attach_id = uuid4()
    blob_path = _blob_path_for(session_id, attach_id)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    tmp_path = blob_path.with_suffix(".tmp")
    tmp_path.write_bytes(data)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, blob_path)
    relative = blob_path.relative_to(attachment_blob_root())

    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        blob_path.unlink(missing_ok=True)
        raise RuntimeError("catalogd is unavailable")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=BLOB_RETENTION_SECS)
    try:
        result = await catalogd.call(
            "session.input.attachment.create.v2",
            {
                "attachment": {
                    "id": str(attach_id),
                    "input_receipt_id": str(receipt_uuid),
                    "owner_id": int(owner_id),
                    "session_id": str(session_id),
                    "mime_type": mime_type,
                    "byte_size": len(data),
                    "sha256": digest,
                    "blob_path": str(relative),
                    "original_filename": (original_filename or "")[:255] or None,
                    "original_byte_size": original_byte_size,
                    "expires_at": expires_at.isoformat(),
                }
            },
            timeout_seconds=1.0,
        )
    except Exception:
        blob_path.unlink(missing_ok=True)
        raise
    root = attachment_blob_root().resolve()
    for raw_path in result.get("pruned_blob_paths") or []:
        candidate = (root / str(raw_path)).resolve()
        if candidate.is_relative_to(root):
            candidate.unlink(missing_ok=True)
    if not result.get("attachment"):
        blob_path.unlink(missing_ok=True)
        raise RuntimeError(str(result.get("reason") or "catalog attachment was not persisted"))
    return StoredAttachment(
        id=attach_id,
        session_input_id=str(receipt_uuid),
        session_id=session_id,
        mime_type=mime_type,
        byte_size=len(data),
        sha256=digest,
        blob_path=blob_path,
        original_filename=(original_filename or "")[:255] or None,
        original_byte_size=original_byte_size,
    )


async def get_catalog_attachment(
    *,
    owner_id: int,
    session_id: UUID,
    input_receipt_id: str,
    attachment_id: UUID,
) -> StoredAttachment | None:
    """Read bounded attachment metadata through catalogd."""

    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalogd = get_catalogd_client()
    if catalogd is None:
        raise RuntimeError("catalogd is unavailable")
    result = await catalogd.call(
        "session.input.attachment.read.v2",
        {
            "owner_id": int(owner_id),
            "session_id": str(session_id),
            "input_receipt_id": str(UUID(input_receipt_id)),
            "attachment_id": str(attachment_id),
        },
        timeout_seconds=1.0,
    )
    row = result.get("attachment")
    if not isinstance(row, dict):
        return None
    return StoredAttachment(
        id=UUID(str(row["id"])),
        session_input_id=str(row["input_receipt_id"]),
        session_id=UUID(str(row["session_id"])),
        mime_type=str(row["mime_type"]),
        byte_size=int(row["byte_size"]),
        sha256=str(row["sha256"]),
        blob_path=attachment_blob_root() / str(row["blob_path"]),
        original_filename=row.get("original_filename"),
        original_byte_size=(int(row["original_byte_size"]) if row.get("original_byte_size") is not None else None),
    )


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
    store_media_blob(
        db,
        sha256=digest,
        mime_type=mime_type,
        data=data,
        first_seen_session_id=session_input.session_id,
        commit=False,
    )
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
    upsert_media_ref(
        db,
        item={
            "sha256": digest,
            "session_id": session_input.session_id,
            "source_path": f"session_input:{int(session_input.id)}",
            "source_offset": 0,
            "json_pointer": f"/attachments/{attach_id}",
            "provider": "longhouse",
            "original_kind": "attachment",
        },
        media_state="present",
    )
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
    return db.query(SessionInputAttachment).filter(SessionInputAttachment.id == attachment_id).first()


def absolute_blob_path(row: SessionInputAttachment) -> Path:
    return attachment_blob_root() / row.blob_path


def read_path_for_attachment(db: Session, row: SessionInputAttachment) -> Path:
    """Return the best available blob path for an attachment row.

    The attachment-specific blob is a delivery cache for the engine fetch path.
    Durable history lives in the shared media store, so old rows can still be
    fetched after the duplicate attachment blob has been reaped.
    """

    path = absolute_blob_path(row)
    if path.is_file():
        return path
    media = db.query(MediaObject).filter(MediaObject.sha256 == row.sha256).first()
    if media is not None:
        media_path = absolute_media_path(media)
        if media_path.is_file():
            return media_path
    return path


def _shared_media_present(db: Session, row: SessionInputAttachment) -> bool:
    media = db.query(MediaObject).filter(MediaObject.sha256 == row.sha256).first()
    return media is not None and absolute_media_path(media).is_file()


def cleanup_stale_blobs(db: Session, *, retention_secs: int = BLOB_RETENTION_SECS) -> int:
    """Delete duplicate delivery blobs whose bytes are durable in media_objects.

    Attachment rows remain as durable composer provenance. We only reap the
    attachment-specific delivery copy after confirming the shared media object
    still exists, so cleanup can never delete the only copy of user-supplied
    session media.
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
        if not _shared_media_present(db, row):
            logger.warning(
                "attachment cleanup: preserving %s because shared media %s is missing",
                row.id,
                row.sha256,
            )
            continue
        path = absolute_blob_path(row)
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("attachment cleanup: failed to unlink %s", path, exc_info=True)
    db.commit()
    if removed:
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
