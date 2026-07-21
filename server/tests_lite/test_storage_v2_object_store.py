from __future__ import annotations

import hashlib
import os
from io import BytesIO
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import ClientError

from zerg.storage_v2.object_store import B2BackupMirrorObjectStore
from zerg.storage_v2.object_store import FilesystemImmutableObjectStore
from zerg.storage_v2.object_store import ObjectStoreCorruptError
from zerg.storage_v2.object_store import ObjectStoreValidationError
from zerg.storage_v2.object_store import S3CompatibleImmutableObjectStore


def real_b2_store_or_skip() -> B2BackupMirrorObjectStore:
    """Return the disposable B2 proof store only when the operator opts in."""

    if os.environ.get("LONGHOUSE_B2_REAL_PROOF") != "1":
        pytest.skip("set LONGHOUSE_B2_REAL_PROOF=1 to run the disposable B2 proof")
    required = ("B2_LONGHOUSE_PHASE3_KEY_ID", "B2_LONGHOUSE_PHASE3_APP_KEY", "B2_LONGHOUSE_PHASE3_BUCKET", "B2_LONGHOUSE_PHASE3_S3_ENDPOINT")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.fail(f"B2 proof credentials are incomplete: {', '.join(missing)}")
    endpoint = os.environ["B2_LONGHOUSE_PHASE3_S3_ENDPOINT"]
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{endpoint}",
        aws_access_key_id=os.environ["B2_LONGHOUSE_PHASE3_KEY_ID"],
        aws_secret_access_key=os.environ["B2_LONGHOUSE_PHASE3_APP_KEY"],
        region_name=endpoint.split(".")[1],
    )
    return B2BackupMirrorObjectStore(
        client,
        bucket=os.environ["B2_LONGHOUSE_PHASE3_BUCKET"],
    )


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


class _S3:
    def __init__(self):
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.conditional_conflicts = 0

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        identity = (kwargs["Bucket"], kwargs["Key"])
        if self.conditional_conflicts:
            self.conditional_conflicts -= 1
            raise ClientError({"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject")
        if kwargs.get("IfNoneMatch") == "*" and identity in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[identity] = {"data": kwargs["Body"], "metadata": kwargs["Metadata"]}
        return {}

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        try:
            object_ = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        except KeyError as exc:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject") from exc
        return {"ContentLength": len(object_["data"]), "Metadata": object_["metadata"]}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        try:
            object_ = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        except KeyError as exc:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from exc
        return {"ContentLength": len(object_["data"]), "Body": BytesIO(object_["data"])}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {}


def test_s3_store_conditionally_mirrors_verifies_and_isolates_tenant_namespaces():
    client = _S3()
    store = S3CompatibleImmutableObjectStore(client, bucket="phase3", namespace="longhouse/v1")
    data = b"remote immutable bytes"
    sha256 = hashlib.sha256(data).hexdigest()
    key = "raw/v2/ab/object.zst"

    first = store.put_if_absent(tenant_id="tenant-a", key=key, data=data, sha256=sha256)
    replay = store.put_if_absent(tenant_id="tenant-a", key=key, data=data, sha256=sha256)
    other = store.put_if_absent(tenant_id="tenant-b", key=key, data=data, sha256=sha256)

    assert first.reused is False and replay.reused is True and other.reused is False
    put_keys = [call[1]["Key"] for call in client.calls if call[0] == "put"]
    assert len(set(put_keys)) == 2
    assert all(key in remote_key and "tenant-a" not in remote_key and "tenant-b" not in remote_key for remote_key in put_keys)
    assert store.read_verified(tenant_id="tenant-a", key=key, sha256=sha256, max_bytes=len(data)) == data
    assert store.delete_verified(tenant_id="tenant-a", key=key, sha256=sha256) is True
    assert store.delete_verified(tenant_id="tenant-a", key=key, sha256=sha256) is False


def test_s3_store_rejects_corrupt_existing_content_and_bounded_read():
    client = _S3()
    store = S3CompatibleImmutableObjectStore(client, bucket="phase3")
    data = b"remote immutable bytes"
    sha256 = hashlib.sha256(data).hexdigest()
    key = "raw/v2/ab/object.zst"
    store.put_if_absent(tenant_id="tenant-a", key=key, data=data, sha256=sha256)
    identity = ("phase3", next(call[1]["Key"] for call in client.calls if call[0] == "put"))
    client.objects[identity]["data"] = b"X" * len(data)

    with pytest.raises(ObjectStoreCorruptError, match="metadata hash mismatch|hash mismatch"):
        store.put_if_absent(tenant_id="tenant-a", key=key, data=data, sha256=sha256)
    with pytest.raises(ObjectStoreCorruptError, match="read bound"):
        store.read_verified(tenant_id="tenant-a", key=key, sha256=sha256, max_bytes=1)
    with pytest.raises(ObjectStoreCorruptError, match="hash mismatch"):
        store.delete_verified(tenant_id="tenant-a", key=key, sha256=sha256)


def test_s3_store_retries_one_conditional_create_race():
    client = _S3()
    client.conditional_conflicts = 1
    store = S3CompatibleImmutableObjectStore(client, bucket="phase3")
    data = b"remote immutable bytes"
    sha256 = hashlib.sha256(data).hexdigest()

    stored = store.put_if_absent(tenant_id="tenant-a", key="raw/v2/ab/object.zst", data=data, sha256=sha256)

    assert stored.reused is False
    assert len([call for call in client.calls if call[0] == "put"]) == 2


@pytest.mark.timeout(60)
def test_real_b2_store_contract() -> None:
    """Exercise immutable create/replay/read/delete against the scoped B2 key."""

    store = real_b2_store_or_skip()
    tenant_id = f"phase3-contract-{uuid4()}"
    data = b"Longhouse Phase 3 B2 contract proof\n"
    sha256 = hashlib.sha256(data).hexdigest()
    key = f"backup/v1/blobs/{sha256[:2]}/{sha256}"

    first = store.put_if_absent(tenant_id=tenant_id, key=key, data=data, sha256=sha256)
    replay = store.put_if_absent(tenant_id=tenant_id, key=key, data=data, sha256=sha256)

    assert first.reused is False and replay.reused is True
    assert store.read_verified(tenant_id=tenant_id, key=key, sha256=sha256, max_bytes=len(data)) == data
    assert store.delete_verified(tenant_id=tenant_id, key=key, sha256=sha256) is True
    assert store.delete_verified(tenant_id=tenant_id, key=key, sha256=sha256) is False
