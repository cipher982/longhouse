"""Opportunistic media backfill from legacy inline data URLs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSourceLine
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef
from zerg.services.media_store import ALLOWED_MEDIA_MIME_TYPES
from zerg.services.media_store import media_blob_root
from zerg.services.media_store import store_media_blob
from zerg.services.media_store import upsert_media_ref
from zerg.services.raw_json_compression import decode_raw_json

DATA_IMAGE_RE = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)")
BASE64_WHITESPACE_DELETE = str.maketrans("", "", "\r\n ")


@dataclass(frozen=True)
class InlineMediaCandidate:
    mime_type: str
    data_url: str
    json_pointer: str | None


@dataclass(frozen=True)
class MediaBackfillResult:
    dry_run: bool
    scanned_source_lines: int
    candidate_refs: int
    decoded_bytes: int
    stored_objects: int
    refs_upserted: int
    skipped_existing_refs: int
    skipped_budget: int
    skipped_disk_floor: int
    rejected: int
    last_source_line_id: int | None


def backfill_inline_data_url_media(
    db: Session,
    *,
    dry_run: bool = True,
    max_rows: int = 100,
    max_bytes: int = 10 * 1024 * 1024,
    after_id: int = 0,
    confirmed_backup_gate: bool = False,
    disk_floor_bytes: int = 1024 * 1024 * 1024,
) -> MediaBackfillResult:
    """Scan bounded legacy source lines and optionally store inline image bytes.

    This is deliberately opportunistic. It never rewrites source rows, never
    blocks live shipping, and requires an explicit backup-gate confirmation
    before it writes bytes.
    """

    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if disk_floor_bytes < 0:
        raise ValueError("disk_floor_bytes must be non-negative")
    if not dry_run and not confirmed_backup_gate:
        raise ValueError("confirmed_backup_gate is required when dry_run=false")

    query = db.query(AgentSourceLine).filter(AgentSourceLine.id > int(after_id))
    rows = query.order_by(AgentSourceLine.id.asc()).limit(int(max_rows)).all()

    scanned = 0
    candidate_refs = 0
    decoded_bytes = 0
    stored_objects = 0
    refs_upserted = 0
    skipped_existing_refs = 0
    skipped_budget = 0
    skipped_disk_floor = 0
    rejected = 0
    last_source_line_id: int | None = None

    for row in rows:
        scanned += 1
        last_source_line_id = int(row.id)
        raw = decode_raw_json(row) or ""
        if "data:image/" not in raw:
            continue

        for candidate in extract_inline_image_candidates(raw):
            mime_type = _normalized_mime_type(candidate.mime_type)
            candidate_refs += 1
            try:
                data = _decode_data_url(candidate, mime_type=mime_type)
            except ValueError:
                rejected += 1
                continue

            if decoded_bytes + len(data) > max_bytes:
                skipped_budget += 1
                continue
            if not dry_run and not _has_disk_floor(media_blob_root(), len(data), disk_floor_bytes):
                skipped_disk_floor += 1
                continue

            sha256 = hashlib.sha256(data).hexdigest()
            decoded_bytes += len(data)

            if dry_run:
                continue

            existing_ref = (
                db.query(SessionMediaRef.id)
                .filter(SessionMediaRef.session_id == row.session_id)
                .filter(SessionMediaRef.media_sha256 == sha256)
                .filter(SessionMediaRef.source_path == row.source_path)
                .filter(SessionMediaRef.source_offset == int(row.source_offset))
                .first()
            )
            ref_changed = upsert_media_ref(
                db,
                item={
                    "session_id": row.session_id,
                    "source_path": row.source_path,
                    "source_offset": int(row.source_offset),
                    "source_line_hash": row.line_hash,
                    "json_pointer": candidate.json_pointer,
                    "provider": getattr(getattr(row, "session", None), "provider", None),
                    "original_kind": "data_url_backfill",
                    "sha256": sha256,
                },
                media_state="pending",
            )
            db.flush()
            stored = store_media_blob(
                db,
                sha256=sha256,
                mime_type=mime_type,
                data=data,
                first_seen_session_id=row.session_id,
                commit=False,
            )
            stored_objects += 1 if stored.created else 0
            refs_upserted += 1 if ref_changed else 0
            skipped_existing_refs += 1 if existing_ref is not None and not ref_changed else 0

    if not dry_run:
        db.commit()

    return MediaBackfillResult(
        dry_run=dry_run,
        scanned_source_lines=scanned,
        candidate_refs=candidate_refs,
        decoded_bytes=decoded_bytes,
        stored_objects=stored_objects,
        refs_upserted=refs_upserted,
        skipped_existing_refs=skipped_existing_refs,
        skipped_budget=skipped_budget,
        skipped_disk_floor=skipped_disk_floor,
        rejected=rejected,
        last_source_line_id=last_source_line_id,
    )


def extract_inline_image_candidates(raw_json: str) -> list[InlineMediaCandidate]:
    """Return inline image data URLs, with JSON pointers when parseable."""

    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return [
            InlineMediaCandidate(mime_type=match.group(1).lower(), data_url=match.group(0), json_pointer=None)
            for match in DATA_IMAGE_RE.finditer(raw_json)
        ]

    candidates: list[InlineMediaCandidate] = []
    _walk_json_for_data_urls(parsed, "", candidates)
    return candidates


def _walk_json_for_data_urls(value: Any, pointer: str, candidates: list[InlineMediaCandidate]) -> None:
    if isinstance(value, str):
        match = DATA_IMAGE_RE.fullmatch(value)
        if match is not None:
            candidates.append(
                InlineMediaCandidate(
                    mime_type=match.group(1).lower(),
                    data_url=value,
                    json_pointer=pointer or "/",
                )
            )
            return
        for match in DATA_IMAGE_RE.finditer(value):
            candidates.append(
                InlineMediaCandidate(
                    mime_type=match.group(1).lower(),
                    data_url=match.group(0),
                    json_pointer=pointer or "/",
                )
            )
        return

    if isinstance(value, list):
        for idx, item in enumerate(value):
            _walk_json_for_data_urls(item, f"{pointer}/{idx}", candidates)
        return

    if isinstance(value, dict):
        for key, item in value.items():
            _walk_json_for_data_urls(item, f"{pointer}/{_escape_json_pointer(str(key))}", candidates)


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _normalized_mime_type(mime_type: str) -> str:
    normalized = mime_type.lower()
    return "image/jpeg" if normalized == "image/jpg" else normalized


def _decode_data_url(candidate: InlineMediaCandidate, *, mime_type: str) -> bytes:
    if mime_type not in ALLOWED_MEDIA_MIME_TYPES:
        raise ValueError("unsupported mime")

    try:
        _header, encoded = candidate.data_url.split(",", 1)
        return base64.b64decode(encoded.translate(BASE64_WHITESPACE_DELETE), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid data url") from exc


def _has_disk_floor(root: Path, incoming_bytes: int, disk_floor_bytes: int) -> bool:
    path = root
    while not path.exists() and path != path.parent:
        path = path.parent
    usage = shutil.disk_usage(path)
    return usage.free - incoming_bytes >= disk_floor_bytes


def compute_media_repair_health(db: Session) -> dict[str, int]:
    """Return separate media repair debt counters for health surfaces."""

    rows = (
        db.query(SessionMediaRef, MediaObject)
        .outerjoin(MediaObject, MediaObject.sha256 == SessionMediaRef.media_sha256)
        .filter(SessionMediaRef.media_state != "present")
        .all()
    )
    return {
        "media_repair_refs": len(rows),
        "media_repair_bytes": sum(int(media.byte_size or 0) for _ref, media in rows if media is not None),
    }
