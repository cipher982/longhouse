"""Tenant-scoped immutable object-store contract and filesystem implementation.

Logical object keys are stable catalog facts.  Backends may enforce a tenant
namespace underneath that logical key, but callers never turn a hash or key into
authorization: every operation carries the authenticated tenant explicitly.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol

from botocore.exceptions import ClientError


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


class S3CompatibleClient(Protocol):
    """Small boto3-compatible surface, kept injectable for contract testing."""

    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...


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


class S3CompatibleImmutableObjectStore:
    """Tenant-isolated S3-compatible implementation for a remote mirror.

    The catalog continues to decide whether an envelope is acknowledged. This
    store only proves bytes under a non-bearer tenant namespace before a caller
    records a mirror receipt.
    """

    def __init__(self, client: S3CompatibleClient, *, bucket: str, namespace: str = "longhouse/v1") -> None:
        if not isinstance(bucket, str) or not bucket:
            raise ObjectStoreValidationError("bucket must be a non-empty string")
        if not isinstance(namespace, str) or not namespace or namespace.startswith("/") or ".." in Path(namespace).parts:
            raise ObjectStoreValidationError("namespace must be a safe relative prefix")
        self.client = client
        self.bucket = bucket
        self.namespace = namespace.rstrip("/")

    def put_if_absent(self, *, tenant_id: str, key: str, data: bytes, sha256: str) -> StoredObject:
        _validate_request(tenant_id=tenant_id, key=key, sha256=sha256)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ObjectStoreCorruptError("object payload hash does not match declared SHA-256")
        remote_key = self._remote_key(tenant_id, key)
        reused = False
        for attempt in range(2):
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=remote_key,
                    Body=data,
                    ContentLength=len(data),
                    ChecksumSHA256=b64encode(bytes.fromhex(sha256)).decode("ascii"),
                    Metadata={"longhouse-sha256": sha256},
                    IfNoneMatch="*",
                )
                break
            except ClientError as exc:
                code = _client_error_code(exc)
                if code in {"412", "PreconditionFailed"}:
                    self._verify_remote_head(tenant_id=tenant_id, key=key, sha256=sha256, size=len(data))
                    reused = True
                    break
                if code not in {"409", "ConditionalRequestConflict"} or attempt:
                    raise ObjectStoreError(f"S3 immutable put failed: {code}") from exc
        else:  # pragma: no cover - loop either succeeds or raises.
            raise AssertionError("unreachable")
        mirrored = self.read_verified(tenant_id=tenant_id, key=key, sha256=sha256, max_bytes=len(data))
        if mirrored != data:
            raise ObjectStoreCorruptError("remote immutable object differs after write")
        return StoredObject(key=key, sha256=sha256, size=len(data), reused=reused)

    def read_verified(self, *, tenant_id: str, key: str, sha256: str, max_bytes: int) -> bytes:
        _validate_request(tenant_id=tenant_id, key=key, sha256=sha256)
        if max_bytes < 0:
            raise ObjectStoreValidationError("max_bytes must be non-negative")
        remote_key = self._remote_key(tenant_id, key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=remote_key, ChecksumMode="ENABLED")
            size = response.get("ContentLength")
            if not isinstance(size, int) or size < 0 or size > max_bytes:
                raise ObjectStoreCorruptError("remote object exceeds its read bound")
            body = response.get("Body")
            if body is None or not hasattr(body, "read"):
                raise ObjectStoreCorruptError("remote object response has no readable body")
            data = body.read(max_bytes + 1)
            close = getattr(body, "close", None)
            if callable(close):
                close()
        except ClientError as exc:
            raise ObjectStoreCorruptError(f"remote object is unreadable: {_client_error_code(exc)}") from exc
        if not isinstance(data, bytes) or len(data) != size or len(data) > max_bytes:
            raise ObjectStoreCorruptError("remote object body is truncated or exceeds its bound")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ObjectStoreCorruptError("remote object hash mismatch")
        return data

    def delete_verified(self, *, tenant_id: str, key: str, sha256: str) -> bool:
        _validate_request(tenant_id=tenant_id, key=key, sha256=sha256)
        try:
            size = self._verify_remote_head(tenant_id=tenant_id, key=key, sha256=sha256, size=None)
            self.read_verified(tenant_id=tenant_id, key=key, sha256=sha256, max_bytes=size)
        except ObjectStoreCorruptError as exc:
            if "404" in str(exc) or "NoSuchKey" in str(exc) or "NotFound" in str(exc):
                return False
            raise
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._remote_key(tenant_id, key))
        except ClientError as exc:
            raise ObjectStoreError(f"S3 immutable delete failed: {_client_error_code(exc)}") from exc
        return True

    def _verify_remote_head(self, *, tenant_id: str, key: str, sha256: str, size: int | None) -> int:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._remote_key(tenant_id, key), ChecksumMode="ENABLED")
        except ClientError as exc:
            raise ObjectStoreCorruptError(f"remote object is unreadable: {_client_error_code(exc)}") from exc
        metadata = response.get("Metadata")
        remote_size = response.get("ContentLength")
        if not isinstance(metadata, dict) or metadata.get("longhouse-sha256") != sha256:
            raise ObjectStoreCorruptError("remote object metadata hash mismatch")
        if not isinstance(remote_size, int) or remote_size < 0 or (size is not None and remote_size != size):
            raise ObjectStoreCorruptError("remote object size mismatch")
        return remote_size

    def _remote_key(self, tenant_id: str, key: str) -> str:
        # Tenant IDs are never object authorization, but hashing makes the
        # namespace opaque in provider inventory and avoids path injection.
        tenant_namespace = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        return f"{self.namespace}/tenants/{tenant_namespace}/{key}"


def _validate_request(*, tenant_id: str, key: str, sha256: str) -> None:
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ObjectStoreValidationError("tenant_id must be a non-empty string")
    if not isinstance(key, str) or not key:
        raise ObjectStoreValidationError("object key must be a non-empty string")
    if not isinstance(sha256, str) or len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ObjectStoreValidationError("sha256 must be lowercase SHA-256 hex")


def _client_error_code(error: ClientError) -> str:
    response = error.response.get("Error", {})
    return str(response.get("Code", "unknown"))


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
    "S3CompatibleImmutableObjectStore",
    "StoredObject",
]
