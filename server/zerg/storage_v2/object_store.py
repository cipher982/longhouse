"""Tenant-scoped immutable object-store contract and filesystem implementation.

Logical object keys are stable catalog facts.  Backends may enforce a tenant
namespace underneath that logical key, but callers never turn a hash or key into
authorization: every operation carries the authenticated tenant explicitly.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ObjectStoreError(RuntimeError):
    """Base error at the backend-neutral immutable object boundary."""


class ObjectStoreValidationError(ObjectStoreError):
    pass


class ObjectStoreCorruptError(ObjectStoreError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    size: int
    reused: bool


class ImmutableObjectStore(Protocol):
    """Operations a remote mirror must implement before it can hold raw data."""

    def put_if_absent(self, *, tenant_id: str, key: str, data: bytes, sha256: str) -> StoredObject: ...

    def read_verified(self, *, tenant_id: str, key: str, sha256: str, max_bytes: int) -> bytes: ...

    def delete_verified(self, *, tenant_id: str, key: str, sha256: str) -> bool: ...


class FilesystemImmutableObjectStore:
    """Self-hosted implementation preserving the existing logical key layout."""

    def __init__(self, root: Path, *, tenant_id: str | None = None) -> None:
        self.root = root.expanduser().resolve()
        if tenant_id is not None and (not isinstance(tenant_id, str) or not tenant_id):
            raise ObjectStoreValidationError("bound tenant_id must be a non-empty string")
        self.tenant_id = tenant_id

    def put_if_absent(self, *, tenant_id: str, key: str, data: bytes, sha256: str) -> StoredObject:
        _validate_request(tenant_id=tenant_id, key=key, sha256=sha256)
        self._assert_tenant(tenant_id)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ObjectStoreCorruptError("object payload hash does not match declared SHA-256")
        final_path = self._path_for(key)
        final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if final_path.exists():
            existing = self.read_verified(tenant_id=tenant_id, key=key, sha256=sha256, max_bytes=len(data))
            if existing != data:
                raise ObjectStoreCorruptError(f"existing immutable object differs: {key}")
            return StoredObject(key=key, sha256=sha256, size=len(data), reused=True)

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{sha256}.tmp-",
                dir=final_path.parent,
                delete=False,
            ) as handle:
                temporary_name = handle.name
                os.chmod(temporary_name, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, final_path)
            temporary_name = None
            _fsync_directory(final_path.parent)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return StoredObject(key=key, sha256=sha256, size=len(data), reused=False)

    def read_verified(self, *, tenant_id: str, key: str, sha256: str, max_bytes: int) -> bytes:
        _validate_request(tenant_id=tenant_id, key=key, sha256=sha256)
        self._assert_tenant(tenant_id)
        if max_bytes < 0:
            raise ObjectStoreValidationError("max_bytes must be non-negative")
        path = self._path_for(key)
        try:
            size = path.stat().st_size
            if size > max_bytes:
                raise ObjectStoreCorruptError("object exceeds its read bound")
            data = path.read_bytes()
        except OSError as exc:
            raise ObjectStoreCorruptError(f"object is unreadable: {key}") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ObjectStoreCorruptError("object hash mismatch")
        return data

    def delete_verified(self, *, tenant_id: str, key: str, sha256: str) -> bool:
        _validate_request(tenant_id=tenant_id, key=key, sha256=sha256)
        self._assert_tenant(tenant_id)
        path = self._path_for(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ObjectStoreCorruptError(f"object is unreadable: {key}") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ObjectStoreCorruptError("refusing to delete object with an unexpected hash")
        path.unlink()
        _fsync_directory(path.parent)
        return True

    def _path_for(self, key: str) -> Path:
        relative = Path(key)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ObjectStoreValidationError("object key must be safe and relative")
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ObjectStoreValidationError("object key escapes storage root")
        return path

    def _assert_tenant(self, tenant_id: str) -> None:
        if self.tenant_id is not None and tenant_id != self.tenant_id:
            raise ObjectStoreCorruptError("object access does not match the bound tenant")


def _validate_request(*, tenant_id: str, key: str, sha256: str) -> None:
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ObjectStoreValidationError("tenant_id must be a non-empty string")
    if not isinstance(key, str) or not key:
        raise ObjectStoreValidationError("object key must be a non-empty string")
    if not isinstance(sha256, str) or len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ObjectStoreValidationError("sha256 must be lowercase SHA-256 hex")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "FilesystemImmutableObjectStore",
    "ImmutableObjectStore",
    "ObjectStoreCorruptError",
    "ObjectStoreError",
    "ObjectStoreValidationError",
    "StoredObject",
]
