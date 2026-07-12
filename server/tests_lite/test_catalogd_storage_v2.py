from __future__ import annotations

import hashlib
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import CatalogBase
from zerg.catalogd.models import RawObject as LiveRawObject
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-storage-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _epoch_params(*, epoch: UUID, opened_at: datetime, predecessor: UUID | None = None) -> dict:
    return {
        "tenant_id": "tenant-a",
        "machine_id": "cinder",
        "provider": "codex",
        "opaque_source_id": "history.jsonl",
        "source_epoch": str(epoch),
        "range_kind": "byte_offset",
        "predecessor_source_epoch": str(predecessor) if predecessor is not None else None,
        "opened_at": opened_at.isoformat(),
    }


def _raw_params(
    *,
    epoch: UUID,
    session_id: UUID,
    start: int,
    end: int,
    records: tuple[bytes, ...],
    sealed_at: datetime,
) -> dict:
    record_hashes = tuple(hashlib.sha256(record).digest() for record in records)
    identity = EnvelopeIdentity(
        tenant_id="tenant-a",
        machine_id="cinder",
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=epoch,
        range_kind="byte_offset",
        range_start=start,
        range_end=end,
        record_hashes=record_hashes,
    )
    envelope = envelope_id(identity)
    payload_hash = hashlib.sha256(b"payload:" + b"".join(records)).hexdigest()
    object_hash = hashlib.sha256(b"compressed:" + b"".join(records)).hexdigest()
    return {
        "protocol_version": 2,
        "tenant_id": "tenant-a",
        "session_id": str(session_id),
        "machine_id": "cinder",
        "provider": "codex",
        "opaque_source_id": "history.jsonl",
        "source_epoch": str(epoch),
        "range_kind": "byte_offset",
        "range_start": start,
        "range_end": end,
        "record_hashes": [value.hex() for value in record_hashes],
        "envelope_id": envelope,
        "object_hash": object_hash,
        "payload_hash": payload_hash,
        "compressed_hash": object_hash,
        "object_path": f"raw/{object_hash[:2]}/{object_hash}.zst",
        "uncompressed_size": sum(len(record) for record in records),
        "compressed_size": max(1, sum(len(record) for record in records) // 2),
        "provenance_kind": "native",
        "render_state": "pending",
        "media_state": "complete",
        "missing_media_hashes": [],
        "sealed_at": sealed_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_source_epoch_raw_manifest_is_idempotent_ordered_and_overlap_safe(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    next_epoch = uuid4()
    session_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        opened = await client.call("storage.source_epoch.open.v2", _epoch_params(epoch=epoch, opened_at=now))
        replay_open = await client.call("storage.source_epoch.open.v2", _epoch_params(epoch=epoch, opened_at=now))
        assert opened["created"] is True and opened["commit_seq"] == "1"
        assert replay_open["exact_replay"] is True and replay_open["commit_seq"] == "1"

        raw = _raw_params(
            epoch=epoch,
            session_id=session_id,
            start=0,
            end=6,
            records=(b"hello\n",),
            sealed_at=now,
        )
        committed = await client.call("storage.raw_object.commit.v2", raw)
        replay = await client.call("storage.raw_object.commit.v2", raw)
        assert committed["created"] is True and committed["receipt"]["commit_seq"] == "2"
        assert replay["exact_replay"] is True and replay["receipt"] == committed["receipt"]
        assert committed["receipt"]["raw_state"] == "durable"

        derived_drift = {
            **raw,
            "object_path": f"compacted/{raw['object_hash']}.zst",
            "render_state": "ready",
            "media_state": "pending",
        }
        replay_after_drift = await client.call("storage.raw_object.commit.v2", derived_drift)
        assert replay_after_drift["exact_replay"] is True
        assert replay_after_drift["receipt"] == committed["receipt"]

        existence = await client.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [raw["envelope_id"], "f" * 64]},
        )
        assert existence["commit_seq"] == "2"
        assert existence["objects"] == [
            {
                "envelope_id": raw["envelope_id"],
                "exists": True,
                "state": "durable",
                "object_hash": raw["object_hash"],
                "commit_seq": "2",
            },
            {
                "envelope_id": "f" * 64,
                "exists": False,
                "state": "missing",
                "object_hash": None,
                "commit_seq": None,
            },
        ]

        identity_mismatch = {**raw, "envelope_id": "0" * 64}
        with pytest.raises(CatalogRemoteError) as invalid_identity:
            await client.call("storage.raw_object.commit.v2", identity_mismatch)
        assert invalid_identity.value.code == "invalid_request"

        same_range_other_content = _raw_params(
            epoch=epoch,
            session_id=session_id,
            start=0,
            end=6,
            records=(b"other\n",),
            sealed_at=now,
        )
        with pytest.raises(CatalogRemoteError) as exact_conflict:
            await client.call("storage.raw_object.commit.v2", same_range_other_content)
        assert exact_conflict.value.code == "source_epoch_conflict"

        partial_overlap = _raw_params(
            epoch=epoch,
            session_id=session_id,
            start=5,
            end=8,
            records=(b"abc",),
            sealed_at=now,
        )
        with pytest.raises(CatalogRemoteError) as overlap_conflict:
            await client.call("storage.raw_object.commit.v2", partial_overlap)
        assert overlap_conflict.value.code == "source_epoch_conflict"

        manifest = await client.call(
            "storage.source_epoch.manifest.v2",
            {"source_epoch": str(epoch), "after_position": None, "limit": 100},
        )
        assert manifest["commit_seq"] == "2"
        assert manifest["source_epoch"]["accepted_through"] == "6"
        assert manifest["source_epoch"]["object_count"] == 1
        assert [row["envelope_id"] for row in manifest["objects"]] == [raw["envelope_id"]]

        replacement = await client.call(
            "storage.source_epoch.open.v2",
            _epoch_params(epoch=next_epoch, opened_at=now, predecessor=epoch),
        )
        assert replacement["commit_seq"] == "3"
        old_manifest = await client.call(
            "storage.source_epoch.manifest.v2",
            {"source_epoch": str(epoch), "after_position": None, "limit": 100},
        )
        assert old_manifest["source_epoch"]["state"] == "closed"
        assert old_manifest["source_epoch"]["replaced_by_source_epoch"] == str(next_epoch)
        closed_epoch_raw = _raw_params(
            epoch=epoch,
            session_id=session_id,
            start=6,
            end=7,
            records=(b"c",),
            sealed_at=now,
        )
        with pytest.raises(CatalogRemoteError) as closed_epoch:
            await client.call("storage.raw_object.commit.v2", closed_epoch_raw)
        assert closed_epoch.value.code == "source_epoch_conflict"

        high_start = (1 << 64) - 2
        high_raw = _raw_params(
            epoch=next_epoch,
            session_id=session_id,
            start=high_start,
            end=high_start + 1,
            records=(b"z",),
            sealed_at=now,
        )
        assert (await client.call("storage.raw_object.commit.v2", high_raw))["receipt"]["commit_seq"] == "4"
        out_of_order = _raw_params(
            epoch=next_epoch,
            session_id=session_id,
            start=0,
            end=1,
            records=(b"o",),
            sealed_at=now,
        )
        with pytest.raises(CatalogRemoteError) as out_of_order_error:
            await client.call("storage.raw_object.commit.v2", out_of_order)
        assert out_of_order_error.value.code == "source_epoch_conflict"

        await client.close()
        await daemon.close()
        daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
        await daemon.start()
        client = CatalogClient(socket_path)
        high_manifest = await client.call(
            "storage.source_epoch.manifest.v2",
            {"source_epoch": str(next_epoch), "after_position": high_start, "limit": 100},
        )
        assert high_manifest["objects"][0]["range_start"] == str(high_start)
        assert high_manifest["source_epoch"]["accepted_through"] == str(high_start + 1)
        assert (await client.call("ping.v2"))["commit_seq"] == "4"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_raw_manifest_honors_session_tombstone_and_retired_receipt(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    deleted_session = uuid4()
    epoch = uuid4()
    with engine.begin() as connection:
        connection.execute(
            LiveSessionTombstone.__table__.insert().values(
                session_id=str(deleted_session),
                deletion_revision=9,
                deleted_at=now,
                commit_seq=1,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        await client.call("storage.source_epoch.open.v2", _epoch_params(epoch=epoch, opened_at=now))
        deleted_raw = _raw_params(
            epoch=epoch,
            session_id=deleted_session,
            start=0,
            end=1,
            records=(b"x",),
            sealed_at=now,
        )
        with pytest.raises(CatalogRemoteError) as deleted:
            await client.call("storage.raw_object.commit.v2", deleted_raw)
        assert deleted.value.code == "session_deleted"
        assert deleted.value.details["deletion_revision"] == "9"

        live_raw = _raw_params(
            epoch=epoch,
            session_id=uuid4(),
            start=0,
            end=1,
            records=(b"y",),
            sealed_at=now,
        )
        await client.call("storage.raw_object.commit.v2", live_raw)
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.begin() as connection:
        connection.execute(
            LiveRawObject.__table__.update()
            .where(LiveRawObject.envelope_id == live_raw["envelope_id"])
            .values(retired_at=now, retirement_revision=10)
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as retired:
            await client.call("storage.raw_object.commit.v2", live_raw)
        assert retired.value.code == "session_deleted"
        assert retired.value.details["deletion_revision"] == "10"
    finally:
        await client.close()
        await daemon.close()


def test_storage_v2_tables_are_catalog_schema_owned(daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    assert {
        "source_epochs",
        "raw_objects",
        "session_tombstones",
        "media_objects",
        "session_media_refs",
        "projector_state",
    }.issubset(set(CatalogBase.metadata.tables))
    with engine.connect() as connection:
        table_names = {
            row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
    engine.dispose()
    assert set(CatalogBase.metadata.tables).issubset(table_names)


def test_existing_v1_catalog_additively_creates_storage_v2_tables(daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        for table_name in CatalogBase.metadata.tables:
            connection.exec_driver_sql(f'DROP TABLE "{table_name}"')

    metadata = initialize_catalog_schema(engine)
    with engine.connect() as connection:
        table_names = {
            row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
    engine.dispose()

    assert metadata.schema_version == 1
    assert set(CatalogBase.metadata.tables).issubset(table_names)
