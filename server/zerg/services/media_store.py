"""Content-addressed archive media blob storage."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef

logger = logging.getLogger(__name__)


ALLOWED_MEDIA_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_MEDIA_BYTES = int(os.getenv("LONGHOUSE_MEDIA_MAX_BYTES", str(32 * 1024 * 1024)))
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class StoredMediaObject:
    sha256: str
    mime_type: str
    byte_size: int
    blob_path: Path
    created: bool


@dataclass(frozen=True)
class MediaClaimResult:
    needed: list[str]
    present: list[str]
    rejected: list[dict[str, str]]


def media_blob_root() -> Path:
    """Return the root for content-addressed archive media blobs."""

    override = os.getenv("LONGHOUSE_MEDIA_BLOB_ROOT")
    if override:
        return Path(override)
    return get_settings().data_dir / "media"


def is_valid_sha256(value: str) -> bool:
    return bool(_SHA256_RE.fullmatch(value))


def validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if not is_valid_sha256(normalized):
        raise ValueError("invalid sha256")
    return normalized


def media_storage_relative_path(sha256: str) -> Path:
    digest = validate_sha256(sha256)
    return Path("objects") / "sha256" / digest[:2] / digest[2:4] / f"{digest}.bin"


def absolute_media_path(row: MediaObject) -> Path:
    return media_blob_root() / row.storage_path


def media_row_is_present(row: MediaObject | None) -> bool:
    if row is None:
        return False
    path = absolute_media_path(row)
    return path.is_file() and path.stat().st_size == int(row.byte_size)


def _ref_query(db: Session, *, item: dict, sha256: str):
    return db.query(SessionMediaRef).filter(
        SessionMediaRef.session_id == item.get("session_id"),
        SessionMediaRef.media_sha256 == sha256,
        SessionMediaRef.source_path == item.get("source_path"),
        SessionMediaRef.source_offset == item.get("source_offset"),
    )


def upsert_media_ref(db: Session, *, item: dict, media_state: str) -> bool:
    """Record where a media object appeared in an archived source line."""

    session_id = item.get("session_id")
    if session_id is None:
        return False

    sha256 = validate_sha256(str(item.get("sha256") or ""))
    source_offset = item.get("source_offset")
    if source_offset is not None:
        source_offset = int(source_offset)
        item["source_offset"] = source_offset

    row = _ref_query(db, item=item, sha256=sha256).first()
    changed = False
    if row is None:
        row = SessionMediaRef(
            session_id=session_id,
            event_id=item.get("event_id"),
            source_path=item.get("source_path"),
            source_offset=source_offset,
            source_line_hash=item.get("source_line_hash"),
            json_pointer=item.get("json_pointer"),
            provider=item.get("provider"),
            original_kind=item.get("original_kind") or "inline_data_url",
            media_sha256=sha256,
            media_state=media_state,
        )
        db.add(row)
        return True

    if row.media_state != "present" and media_state == "present":
        row.media_state = "present"
        row.last_error = None
        changed = True
    if item.get("event_id") is not None and row.event_id != item.get("event_id"):
        row.event_id = item.get("event_id")
        changed = True
    if item.get("source_line_hash") and row.source_line_hash != item.get("source_line_hash"):
        row.source_line_hash = item.get("source_line_hash")
        changed = True
    if item.get("json_pointer") and row.json_pointer != item.get("json_pointer"):
        row.json_pointer = item.get("json_pointer")
        changed = True
    if row.provider is None and item.get("provider"):
        row.provider = item.get("provider")
        changed = True
    if row.original_kind != (item.get("original_kind") or row.original_kind):
        row.original_kind = item.get("original_kind") or row.original_kind
        changed = True
    return changed


def mark_media_refs_present(db: Session, sha256: str) -> bool:
    """Mark all refs for a now-present blob as available."""

    digest = validate_sha256(sha256)
    rows = db.query(SessionMediaRef).filter(SessionMediaRef.media_sha256 == digest).all()
    changed = False
    for row in rows:
        if row.media_state != "present" or row.last_error is not None:
            row.media_state = "present"
            row.last_error = None
            changed = True
    return changed


def _first_seen_from_refs(db: Session, sha256: str) -> UUID | None:
    ref = (
        db.query(SessionMediaRef)
        .filter(SessionMediaRef.media_sha256 == sha256)
        .order_by(SessionMediaRef.created_at.asc(), SessionMediaRef.id.asc())
        .first()
    )
    return ref.session_id if ref is not None else None


def claim_media(db: Session, items: list[dict]) -> MediaClaimResult:
    """Return which requested sha256 objects are missing from the server."""

    needed: list[str] = []
    present: list[str] = []
    rejected: list[dict[str, str]] = []
    seen_response: set[str] = set()
    refs_changed = False

    for item in items:
        raw_sha = str(item.get("sha256") or "").strip().lower()

        if not is_valid_sha256(raw_sha):
            rejected.append({"sha256": raw_sha, "reason": "invalid_sha256"})
            continue

        byte_size = item.get("byte_size")
        if byte_size is not None:
            try:
                size = int(byte_size)
            except (TypeError, ValueError):
                rejected.append({"sha256": raw_sha, "reason": "invalid_byte_size"})
                continue
            if size <= 0 or size > MAX_MEDIA_BYTES:
                rejected.append({"sha256": raw_sha, "reason": "unsupported_byte_size"})
                continue

        mime_type = item.get("mime_type")
        if mime_type and str(mime_type).split(";", 1)[0].strip().lower() not in ALLOWED_MEDIA_MIME_TYPES:
            rejected.append({"sha256": raw_sha, "reason": "unsupported_mime_type"})
            continue

        row = db.query(MediaObject).filter(MediaObject.sha256 == raw_sha).first()
        is_present = media_row_is_present(row)
        refs_changed = (
            upsert_media_ref(
                db,
                item={**item, "sha256": raw_sha},
                media_state="present" if is_present else "pending",
            )
            or refs_changed
        )
        if raw_sha in seen_response:
            continue
        seen_response.add(raw_sha)
        if is_present:
            present.append(raw_sha)
        else:
            needed.append(raw_sha)

    if refs_changed:
        db.commit()

    return MediaClaimResult(needed=needed, present=present, rejected=rejected)


def store_media_blob(
    db: Session,
    *,
    sha256: str,
    mime_type: str,
    data: bytes,
    first_seen_session_id: UUID | None = None,
    width: int | None = None,
    height: int | None = None,
) -> StoredMediaObject:
    """Persist media bytes once and upsert their content-addressed row."""

    digest = validate_sha256(sha256)
    normalized_mime = mime_type.split(";", 1)[0].strip().lower()
    if normalized_mime not in ALLOWED_MEDIA_MIME_TYPES:
        raise ValueError(f"unsupported mime: {normalized_mime}")
    if len(data) == 0:
        raise ValueError("empty media")
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError(f"media exceeds {MAX_MEDIA_BYTES} bytes")

    actual = hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise ValueError("sha256 mismatch")

    existing = db.query(MediaObject).filter(MediaObject.sha256 == digest).first()
    if media_row_is_present(existing):
        changed = False
        if existing.first_seen_session_id is None and first_seen_session_id is not None:
            existing.first_seen_session_id = first_seen_session_id
            changed = True
        changed = mark_media_refs_present(db, digest) or changed
        if changed:
            db.commit()
            db.refresh(existing)
        return StoredMediaObject(
            sha256=digest,
            mime_type=existing.mime_type,
            byte_size=int(existing.byte_size),
            blob_path=absolute_media_path(existing),
            created=False,
        )

    root = media_blob_root()
    relative = media_storage_relative_path(digest)
    blob_path = root / relative
    blob_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = blob_path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    tmp_path.write_bytes(data)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, blob_path)

    row = existing or MediaObject(sha256=digest)
    row.mime_type = normalized_mime
    row.byte_size = len(data)
    row.width = width
    row.height = height
    row.storage_path = str(relative)
    if row.first_seen_session_id is None and first_seen_session_id is not None:
        row.first_seen_session_id = first_seen_session_id
    elif row.first_seen_session_id is None:
        row.first_seen_session_id = _first_seen_from_refs(db, digest)
    mark_media_refs_present(db, digest)
    db.add(row)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.query(MediaObject).filter(MediaObject.sha256 == digest).first()
        if media_row_is_present(winner):
            return StoredMediaObject(
                sha256=digest,
                mime_type=winner.mime_type,
                byte_size=int(winner.byte_size),
                blob_path=absolute_media_path(winner),
                created=False,
            )
        raise
    except Exception:
        db.rollback()
        logger.warning("media store: database commit failed after writing %s", blob_path, exc_info=True)
        raise
    db.refresh(row)

    return StoredMediaObject(
        sha256=row.sha256,
        mime_type=row.mime_type,
        byte_size=int(row.byte_size),
        blob_path=absolute_media_path(row),
        created=True,
    )
