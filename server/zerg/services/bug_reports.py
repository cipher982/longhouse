"""Durable, owner-scoped bug report bundles for phone-to-Console repair."""

from __future__ import annotations

import errno
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


def _report_error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _bundle_from_manifest(report_id: str, *, owner_id: int) -> BugReportBundle:
    payload = read_manifest(report_id, owner_id=owner_id)
    files = tuple(
        BugReportFile(
            name=str(item["name"]),
            mime_type=str(item["mime_type"]),
            byte_size=int(item["byte_size"]),
            sha256=str(item["sha256"]),
            kind=str(item["kind"]),
        )
        for item in payload.get("files", [])
    )
    return BugReportBundle(
        report_id=str(payload["report_id"]),
        owner_id=int(payload["owner_id"]),
        created_at=str(payload["created_at"]),
        source_session_id=payload.get("source_session_id"),
        files=files,
    )


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


def _replay_bundle_or_conflict(
    report_id: str,
    *,
    owner_id: int,
    payload_sha256: str,
) -> BugReportBundle:
    try:
        manifest = read_manifest(report_id, owner_id=owner_id)
    except (FileNotFoundError, ValueError) as exc:
        raise _report_error("report_id_conflict", "The report could not be reused.", status.HTTP_409_CONFLICT) from exc
    stored_sha256 = str(manifest.get("payload_sha256") or "")
    if stored_sha256 and stored_sha256 != payload_sha256:
        raise _report_error(
            "report_id_conflict",
            "This report id was already used for different evidence.",
            status.HTTP_409_CONFLICT,
        )
    return _bundle_from_manifest(report_id, owner_id=owner_id)


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


def _payload_sha256(
    description: str,
    context_bytes: bytes,
    source_session_id: str | None,
    uploads: list[BugReportUpload],
) -> str:
    digest = hashlib.sha256()
    for value in (
        description.encode("utf-8"),
        context_bytes,
        (source_session_id or "").encode("utf-8"),
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    for upload in uploads:
        mime_type = upload.mime_type.split(";", 1)[0].strip().lower().encode("ascii")
        digest.update(len(mime_type).to_bytes(8, "big"))
        digest.update(mime_type)
        digest.update(len(upload.data).to_bytes(8, "big"))
        digest.update(upload.data)
    return digest.hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _validate_description(description: str) -> str:
    if not isinstance(description, str):
        raise _report_error("report_description_required", "A bug description is required.", status.HTTP_400_BAD_REQUEST)
    value = description.strip()
    if not value:
        raise _report_error("report_description_required", "A bug description is required.", status.HTTP_400_BAD_REQUEST)
    if len(value) > MAX_REPORT_DESCRIPTION_CHARS:
        raise _report_error(
            "report_description_too_long",
            "The bug description is too long.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    return value


def _validate_context(context_json: str) -> bytes:
    try:
        parsed = json.loads(context_json or "{}")
    except json.JSONDecodeError as exc:
        raise _report_error("report_context_invalid", "The report context is invalid.", status.HTTP_400_BAD_REQUEST) from exc
    if not isinstance(parsed, dict):
        raise _report_error("report_context_invalid", "The report context is invalid.", status.HTTP_400_BAD_REQUEST)
    encoded = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REPORT_CONTEXT_BYTES:
        raise _report_error(
            "report_context_too_large",
            "The report context is too large.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
    return encoded


def _validate_uploads(uploads: list[BugReportUpload]) -> None:
    if len(uploads) > MAX_REPORT_FILES:
        raise _report_error(
            "report_too_many_files",
            f"Too many report images (maximum {MAX_REPORT_FILES}).",
            status.HTTP_400_BAD_REQUEST,
        )
    total = 0
    for upload in uploads:
        mime_type = upload.mime_type.split(";", 1)[0].strip().lower()
        if mime_type not in ALLOWED_REPORT_MIME_TYPES:
            raise _report_error(
                "report_unsupported_media",
                f"Unsupported report image type: {mime_type}.",
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        if not upload.data:
            raise _report_error(
                "report_image_empty",
                "An attached image is empty.",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if len(upload.data) > MAX_REPORT_FILE_BYTES:
            raise _report_error(
                "report_image_too_large",
                "An attached image is too large.",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        total += len(upload.data)
    if total > MAX_REPORT_TOTAL_BYTES:
        raise _report_error(
            "report_too_large",
            "The bug report is too large.",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )


def create_bug_report(
    *,
    owner_id: int,
    description: str,
    context_json: str,
    source_session_id: str | None,
    uploads: list[BugReportUpload],
    client_report_id: str | None = None,
) -> BugReportBundle:
    """Validate and atomically publish one immutable, replayable bundle."""

    clean_description = _validate_description(description)
    context_bytes = _validate_context(context_json)
    _validate_uploads(uploads)
    description_data = clean_description.encode("utf-8")
    payload_sha256 = _payload_sha256(clean_description, context_bytes, source_session_id, uploads)
    if len(description_data) + len(context_bytes) + sum(len(upload.data) for upload in uploads) > MAX_REPORT_TOTAL_BYTES:
        raise _report_error("report_too_large", "The bug report is too large.", status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    if client_report_id:
        try:
            report_id = str(UUID(client_report_id))
        except (TypeError, ValueError) as exc:
            raise _report_error("report_id_invalid", "The report could not be identified.", status.HTTP_400_BAD_REQUEST) from exc
    else:
        report_id = str(uuid4())
    root = bug_report_root()
    root.mkdir(parents=True, exist_ok=True)
    final_dir = root / report_id
    if final_dir.exists():
        return _replay_bundle_or_conflict(report_id, owner_id=owner_id, payload_sha256=payload_sha256)
    created_at = datetime.now(timezone.utc).isoformat()
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
            "payload_sha256": payload_sha256,
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
        try:
            os.replace(temporary_dir, final_dir)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR}:
                raise
            shutil.rmtree(temporary_dir, ignore_errors=True)
            return _replay_bundle_or_conflict(report_id, owner_id=owner_id, payload_sha256=payload_sha256)
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
