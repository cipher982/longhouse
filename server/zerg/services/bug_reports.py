"""Durable, owner-scoped bug report bundles for phone-to-Console repair."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

from fastapi import HTTPException
from starlette import status

from zerg.config import get_settings

ALLOWED_REPORT_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_REPORT_FILE_BYTES = 2 * 1024 * 1024
MAX_REPORT_TOTAL_BYTES = 8 * 1024 * 1024
MAX_REPORT_FILES = 4
MAX_REPORT_DESCRIPTION_CHARS = 10_000
MAX_REPORT_CONTEXT_BYTES = 128 * 1024
_REPORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class BugReportUpload:
    filename: str
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class BugReportFile:
    name: str
    mime_type: str
    byte_size: int
    sha256: str
    kind: str


@dataclass(frozen=True)
class BugReportBundle:
    report_id: str
    owner_id: int
    created_at: str
    source_session_id: str | None
    files: tuple[BugReportFile, ...]


def bug_report_root() -> Path:
    override = os.getenv("LONGHOUSE_BUG_REPORT_ROOT")
    if override:
        return Path(override)
    return get_settings().data_dir / "bug-reports"


def _report_dir(report_id: str) -> Path:
    try:
        normalized = str(UUID(str(report_id)))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid report id") from exc
    return bug_report_root() / normalized


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _validate_description(description: str) -> str:
    if not isinstance(description, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="description is required")
    value = description.strip()
    if not value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="description is required")
    if len(value) > MAX_REPORT_DESCRIPTION_CHARS:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="description is too long")
    return value


def _validate_context(context_json: str) -> bytes:
    try:
        parsed = json.loads(context_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="context_json must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="context_json must be an object")
    encoded = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REPORT_CONTEXT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="context_json is too large")
    return encoded


def _validate_uploads(uploads: list[BugReportUpload]) -> None:
    if len(uploads) > MAX_REPORT_FILES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"too many report images (max {MAX_REPORT_FILES})")
    total = 0
    for upload in uploads:
        mime_type = upload.mime_type.split(";", 1)[0].strip().lower()
        if mime_type not in ALLOWED_REPORT_MIME_TYPES:
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"unsupported report image type: {mime_type}")
        if not upload.data or len(upload.data) > MAX_REPORT_FILE_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="report image is empty or too large")
        total += len(upload.data)
    if total > MAX_REPORT_TOTAL_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="bug report is too large")


def create_bug_report(
    *,
    owner_id: int,
    description: str,
    context_json: str,
    source_session_id: str | None,
    uploads: list[BugReportUpload],
) -> BugReportBundle:
    """Validate and atomically publish one immutable report bundle."""

    clean_description = _validate_description(description)
    context_bytes = _validate_context(context_json)
    _validate_uploads(uploads)
    if len(clean_description.encode("utf-8")) + len(context_bytes) + sum(len(upload.data) for upload in uploads) > MAX_REPORT_TOTAL_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="bug report is too large")
    report_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    root = bug_report_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{report_id}.", dir=str(root)))
    files: list[BugReportFile] = []
    try:
        description_data = clean_description.encode("utf-8")
        context_name = "context.json"
        description_name = "description.md"
        _write_private(temporary_dir / description_name, description_data)
        files.append(BugReportFile(description_name, "text/markdown", len(description_data), _sha256(description_data), "description"))
        _write_private(temporary_dir / context_name, context_bytes)
        files.append(BugReportFile(context_name, "application/json", len(context_bytes), _sha256(context_bytes), "context"))
        for index, upload in enumerate(uploads):
            mime_type = upload.mime_type.split(";", 1)[0].strip().lower()
            extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[mime_type]
            name = f"image-{index}.{extension}"
            _write_private(temporary_dir / name, upload.data)
            files.append(BugReportFile(name, mime_type, len(upload.data), _sha256(upload.data), "image"))

        manifest = {
            "schema_version": 1,
            "report_id": report_id,
            "owner_id": int(owner_id),
            "created_at": created_at,
            "source_session_id": source_session_id,
            "files": [
                {
                    "name": item.name,
                    "mime_type": item.mime_type,
                    "byte_size": item.byte_size,
                    "sha256": item.sha256,
                    "kind": item.kind,
                }
                for item in files
            ],
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        _write_private(temporary_dir / "manifest.json", manifest_bytes)
        final_dir = root / report_id
        os.replace(temporary_dir, final_dir)
    except BaseException:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise
    return BugReportBundle(report_id, int(owner_id), created_at, source_session_id, tuple(files))


def read_manifest(report_id: str, *, owner_id: int) -> dict[str, Any]:
    directory = _report_dir(report_id)
    try:
        payload = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise FileNotFoundError(report_id) from exc
    if int(payload.get("owner_id", -1)) != int(owner_id):
        raise FileNotFoundError(report_id)
    return payload


def read_report_file(report_id: str, *, owner_id: int, filename: str) -> tuple[Path, dict[str, Any]]:
    manifest = read_manifest(report_id, owner_id=owner_id)
    if not _REPORT_NAME_RE.fullmatch(filename) or filename in {".", ".."}:
        raise FileNotFoundError(filename)
    entry = next((item for item in manifest.get("files", []) if item.get("name") == filename), None)
    if entry is None:
        raise FileNotFoundError(filename)
    path = _report_dir(report_id) / filename
    if not path.is_file() or path.stat().st_size != int(entry.get("byte_size", -1)):
        raise FileNotFoundError(filename)
    return path, entry
