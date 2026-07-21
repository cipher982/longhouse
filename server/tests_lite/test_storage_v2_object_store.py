from __future__ import annotations

import hashlib

import pytest

from zerg.storage_v2.object_store import FilesystemImmutableObjectStore
from zerg.storage_v2.object_store import ObjectStoreCorruptError
from zerg.storage_v2.object_store import ObjectStoreValidationError


def test_filesystem_store_is_tenant_scoped_idempotent_and_hash_verified(tmp_path):
    store = FilesystemImmutableObjectStore(tmp_path, tenant_id="tenant-a")
    data = b"immutable bytes"
    sha256 = hashlib.sha256(data).hexdigest()

    first = store.put_if_absent(tenant_id="tenant-a", key="raw/v2/ab/object.zst", data=data, sha256=sha256)
    replay = store.put_if_absent(tenant_id="tenant-a", key="raw/v2/ab/object.zst", data=data, sha256=sha256)

    assert first.reused is False
    assert replay.reused is True
    assert store.read_verified(tenant_id="tenant-a", key=first.key, sha256=sha256, max_bytes=len(data)) == data
    with pytest.raises(ObjectStoreCorruptError, match="bound tenant"):
        store.read_verified(tenant_id="tenant-b", key=first.key, sha256=sha256, max_bytes=len(data))


def test_filesystem_store_rejects_unsafe_keys_hash_mismatch_and_unbounded_reads(tmp_path):
    store = FilesystemImmutableObjectStore(tmp_path, tenant_id="tenant-a")
    data = b"immutable bytes"
    sha256 = hashlib.sha256(data).hexdigest()

    with pytest.raises(ObjectStoreValidationError, match="safe and relative"):
        store.put_if_absent(tenant_id="tenant-a", key="../escape", data=data, sha256=sha256)
    with pytest.raises(ObjectStoreCorruptError, match="declared SHA-256"):
        store.put_if_absent(tenant_id="tenant-a", key="raw/v2/ab/object.zst", data=data, sha256="0" * 64)

    store.put_if_absent(tenant_id="tenant-a", key="raw/v2/ab/object.zst", data=data, sha256=sha256)
    with pytest.raises(ObjectStoreCorruptError, match="read bound"):
        store.read_verified(tenant_id="tenant-a", key="raw/v2/ab/object.zst", sha256=sha256, max_bytes=len(data) - 1)


def test_filesystem_store_deletes_only_the_verified_object(tmp_path):
    store = FilesystemImmutableObjectStore(tmp_path, tenant_id="tenant-a")
    data = b"immutable bytes"
    sha256 = hashlib.sha256(data).hexdigest()
    key = "raw/v2/ab/object.zst"
    store.put_if_absent(tenant_id="tenant-a", key=key, data=data, sha256=sha256)

    assert store.delete_verified(tenant_id="tenant-a", key=key, sha256=sha256) is True
    assert store.delete_verified(tenant_id="tenant-a", key=key, sha256=sha256) is False
