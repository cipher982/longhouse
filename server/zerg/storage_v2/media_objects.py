"""Hash-verified immutable media objects for storage-v2."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_MEDIA_BYTES = 32 * 1024 * 1024


class MediaObjectError(RuntimeError):
    """Base error for the immutable media boundary."""


class MediaObjectValidationError(MediaObjectError):
    pass


class MediaObjectCorruptError(MediaObjectError):
    pass


@dataclass(frozen=True, slots=True)
class MediaObjectSpec:
    media_hash: str
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class SealedMediaObject:
    media_hash: str
    mime_type: str
    byte_size: int
    object_path: str
    reused: bool


@dataclass(frozen=True, slots=True)
class DecodedMediaObject:
    media_hash: str
    data: bytes


def seal_media_object(root: Path, spec: MediaObjectSpec) -> SealedMediaObject:
    validate_media_object_spec(spec)
    relative_path = _relative_path(spec.media_hash)
    final_path = _safe_path(root, relative_path)
    final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    if final_path.exists():
        existing = _read_verified(final_path, spec.media_hash)
        if existing != spec.data:
            raise MediaObjectCorruptError(f"existing content-addressed media object is corrupt: {relative_path}")
        reused = True
    else:
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{spec.media_hash}.tmp-",
                dir=final_path.parent,
                delete=False,
            ) as handle:
                temporary_name = handle.name
                os.chmod(temporary_name, 0o600)
                handle.write(spec.data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, final_path)
            temporary_name = None
            _fsync_directory(final_path.parent)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        reused = False

    return SealedMediaObject(
        media_hash=spec.media_hash,
        mime_type=spec.mime_type,
        byte_size=len(spec.data),
        object_path=relative_path.as_posix(),
        reused=reused,
    )


def read_media_object(root: Path, object_path: str, *, expected_media_hash: str) -> DecodedMediaObject:
    if not _is_hash(expected_media_hash):
        raise MediaObjectValidationError("expected_media_hash must be lowercase SHA-256 hex")
    relative_path = Path(object_path)
    if relative_path != _relative_path(expected_media_hash):
        raise MediaObjectValidationError("media object path is not canonical for its hash")
    data = _read_verified(_safe_path(root, relative_path), expected_media_hash)
    return DecodedMediaObject(media_hash=expected_media_hash, data=data)


def validate_media_object_spec(spec: MediaObjectSpec) -> None:
    if not _is_hash(spec.media_hash):
        raise MediaObjectValidationError("media_hash must be lowercase SHA-256 hex")
    if not spec.mime_type or len(spec.mime_type.encode("utf-8")) > 255:
        raise MediaObjectValidationError("mime_type must be a bounded non-empty string")
    if not spec.data:
        raise MediaObjectValidationError("media object cannot be empty")
    if len(spec.data) > MAX_MEDIA_BYTES:
        raise MediaObjectValidationError(f"media object exceeds {MAX_MEDIA_BYTES} bytes")
    if hashlib.sha256(spec.data).hexdigest() != spec.media_hash:
        raise MediaObjectValidationError("media object SHA-256 mismatch")


def _read_verified(path: Path, expected_hash: str) -> bytes:
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size <= 0 or stat.st_size > MAX_MEDIA_BYTES:
            raise MediaObjectCorruptError("media object size is invalid")
        data = path.read_bytes()
    except OSError as exc:
        raise MediaObjectCorruptError("media object is unreadable") from exc
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise MediaObjectCorruptError("media object SHA-256 mismatch")
    return data


def _relative_path(media_hash: str) -> Path:
    return Path("media") / "v2" / "sha256" / media_hash[:2] / media_hash[2:4] / f"{media_hash}.bin"


def _safe_path(root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise MediaObjectValidationError("media object path must be relative")
    resolved_root = root.expanduser().resolve()
    resolved = (resolved_root / relative_path).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise MediaObjectValidationError("media object path escapes storage root")
    return resolved


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


__all__ = [
    "DecodedMediaObject",
    "MAX_MEDIA_BYTES",
    "MediaObjectCorruptError",
    "MediaObjectSpec",
    "MediaObjectValidationError",
    "SealedMediaObject",
    "read_media_object",
    "seal_media_object",
    "validate_media_object_spec",
]
