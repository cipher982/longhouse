from __future__ import annotations

import hashlib
import json
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
    predecessor: UUID | None = None,
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
        "owner_id": "42",
        "session_id": str(session_id),
        "machine_id": "cinder",
        "provider": "codex",
        "opaque_source_id": "history.jsonl",
        "source_epoch": str(epoch),
        "predecessor_source_epoch": str(predecessor) if predecessor is not None else None,
        "epoch_opened_at": sealed_at.isoformat(),
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
        "media_refs": [],
        "projectors": ["render-v2", "search-v2", "worklog-v2"],
        "render_manifest": None,
        "session_facts": {
            "environment": "local",
            "project": "longhouse",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": sealed_at.isoformat(),
            "last_activity_at": sealed_at.isoformat(),
            "ended_at": None,
            "origin_kind": "shadow",
            "hidden_from_default_timeline": False,
            "launch_actor": None,
            "launch_surface": None,
        },
        "sealed_at": sealed_at.isoformat(),
    }


def _render_manifest(generation_id: UUID, *, seed: bytes = b"render-object", position: int = 0) -> dict:
    object_hash = hashlib.sha256(seed).hexdigest()
    first_key = json.dumps(
        [
            1_700_000_000_000_000 + position,
            "cinder",
            "codex",
            "history.jsonl",
            "018f0c3a-7b2d-7f10-8a11-123456789abc",
            position,
            0,
        ],
        separators=(",", ":"),
    )
    return {
        "generation_id": str(generation_id),
        "parser_revision": "engine-parser-v2",
        "ordering_revision": "semantic-order-v2",
        "object_id": object_hash,
        "object_hash": object_hash,
        "payload_hash": hashlib.sha256(b"render-payload").hexdigest(),
        "object_path": f"render/v2/{object_hash[:2]}/{object_hash}.zst",
        "uncompressed_size": 100,
        "compressed_size": 80,
        "event_count": 1,
        "first_order_key": first_key,
        "last_order_key": first_key,
        "user_messages": 1,
        "assistant_messages": 0,
        "tool_calls": 0,
        "first_user_message_preview": "Build it",
        "last_visible_text_preview": "Build it",
    }


@pytest.mark.asyncio
async def test_ready_render_manifest_switches_generation_with_raw_receipt(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    generation_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        raw = _raw_params(epoch=epoch, session_id=session_id, start=0, end=6, records=(b"hello\n",), sealed_at=now)
        raw.update(render_state="ready", render_manifest=_render_manifest(generation_id))
        committed = await client.call("storage.raw_object.commit.v2", raw)
        assert committed["receipt"]["render_state"] == "ready"
        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert session["session"]["current_render_generation"] == str(generation_id)
        assert session["session"]["user_messages"] == 1
        assert session["session"]["first_user_message_preview"] == "Build it"
        timeline = await client.call(
            "storage.session.timeline.list.v2",
            {
                "owner_id": "42",
                "before_last_activity_at": None,
                "before_session_id": None,
                "project": None,
                "provider": None,
                "include_test": False,
                "limit": 10,
            },
        )
        assert [row["session_id"] for row in timeline["sessions"]] == [str(session_id)]
        assert timeline["has_more"] is False
        render = await client.call(
            "storage.session.render_manifest.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "after_order_key": None,
                "limit": 100,
            },
        )
        assert render["stale_generation"] is False
        assert render["generation"]["state"] == "current"
        assert render["objects"][0]["source_envelope_id"] == raw["envelope_id"]
        exhausted = await client.call(
            "storage.session.render_manifest.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "after_order_key": render["objects"][0]["last_order_key"],
                "limit": 100,
            },
        )
        assert exhausted["objects"] == []
        assert exhausted["objects_truncated"] is False
        stale = await client.call(
            "storage.session.render_manifest.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(uuid4()),
                "after_order_key": None,
                "limit": 100,
            },
        )
        assert stale["stale_generation"] is True
        assert stale["current_generation_id"] == str(generation_id)
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_render_object_projection_pages_are_frozen_at_claimed_revision(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    generation_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        first = _raw_params(epoch=epoch, session_id=session_id, start=0, end=6, records=(b"hello\n",), sealed_at=now)
        first.update(render_state="ready", render_manifest=_render_manifest(generation_id, seed=b"first", position=0))
        first_commit = await client.call("storage.raw_object.commit.v2", first)
        first_revision = first_commit["receipt"]["commit_seq"]

        second = _raw_params(epoch=epoch, session_id=session_id, start=6, end=12, records=(b"world\n",), sealed_at=now)
        second.update(render_state="ready", render_manifest=_render_manifest(generation_id, seed=b"second", position=6))
        second_commit = await client.call("storage.raw_object.commit.v2", second)
        second_revision = second_commit["receipt"]["commit_seq"]

        frozen = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": None,
                "snapshot_revision": int(first_revision),
                "after_object_id": None,
                "limit": 100,
            },
        )
        assert frozen["generation_id"] == str(generation_id)
        assert frozen["snapshot_object_count"] == 1
        assert frozen["snapshot_event_count"] == 1
        assert [row["source_envelope_id"] for row in frozen["objects"]] == [first["envelope_id"]]

        first_page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(generation_id),
                "snapshot_revision": int(second_revision),
                "after_object_id": None,
                "limit": 1,
            },
        )
        assert first_page["snapshot_object_count"] == 2
        assert first_page["snapshot_event_count"] == 2
        assert first_page["has_more"] is True
        second_page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(generation_id),
                "snapshot_revision": int(second_revision),
                "after_object_id": first_page["objects"][-1]["object_id"],
                "limit": 1,
            },
        )
        assert second_page["has_more"] is False
        assert {first_page["objects"][0]["object_id"], second_page["objects"][0]["object_id"]} == {
            first["render_manifest"]["object_id"],
            second["render_manifest"]["object_id"],
        }
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_raw_receipt_derives_explicit_missing_media_and_records_envelope_ref(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    media_hash = "d" * 64
    raw = _raw_params(
        epoch=UUID("018f0c3a-7b2d-7f10-8a11-523456789abc"),
        session_id=session_id,
        start=0,
        end=6,
        records=(b"hello\n",),
        sealed_at=now,
    )
    raw["media_refs"] = [
        {
            "media_hash": media_hash,
            "source_position": 0,
            "ref_key": "external-reference:0",
            "availability": "missing",
        }
    ]
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        committed = await client.call("storage.raw_object.commit.v2", raw)
        assert committed["receipt"]["media_state"] == "missing"
        assert committed["receipt"]["missing_media_hashes"] == [media_hash]
        replay = await client.call("storage.raw_object.commit.v2", raw)
        assert replay["exact_replay"] is True
        assert replay["receipt"] == committed["receipt"]

        manifest = await client.call(
            "storage.media.read.v2",
            {"media_hash": media_hash, "session_id": str(session_id), "limit": 10},
        )
        assert manifest["media"]["state"] == "missing"
        assert manifest["refs"][0]["envelope_id"] == raw["envelope_id"]

        drift = {**raw, "media_refs": []}
        with pytest.raises(CatalogRemoteError) as conflict:
            await client.call("storage.raw_object.commit.v2", drift)
        assert conflict.value.code == "source_epoch_conflict"
    finally:
        await client.close()
        await daemon.close()


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

        storage_session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert storage_session["found"] is True
        assert storage_session["session"]["project"] == "longhouse"
        assert storage_session["session"]["raw_state"] == "durable"
        assert storage_session["session"]["transcript_revision"] == committed["receipt"]["commit_seq"]

        session_manifest = await client.call(
            "storage.session.raw_manifest.v2",
            {"session_id": str(session_id), "owner_id": "42", "after_source_key": None, "limit": 100},
        )
        assert session_manifest["found"] is True
        assert session_manifest["objects"][0]["object_path"] == raw["object_path"]
        assert session_manifest["objects"][0]["source_epoch"] == str(epoch)
        raw_row = session_manifest["objects"][0]
        after_source_key = json.dumps(
            [
                raw_row["machine_id"],
                raw_row["provider"],
                raw_row["opaque_source_id"],
                raw_row["source_epoch"],
                f"{int(raw_row['range_start']):020d}",
                raw_row["envelope_id"],
            ],
            separators=(",", ":"),
        )
        exhausted_raw = await client.call(
            "storage.session.raw_manifest.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "after_source_key": after_source_key,
                "limit": 100,
            },
        )
        assert exhausted_raw["objects"] == []

        projector_lag = await client.call(
            "projector.state.list_lag.v2",
            {"projector": "render-v2", "after_session_id": None, "limit": 100},
        )
        assert projector_lag["states"][0]["session_id"] == str(session_id)
        assert projector_lag["states"][0]["desired_revision"] == committed["receipt"]["commit_seq"]

        derived_drift = {
            **raw,
            "object_path": f"compacted/{raw['object_hash']}.zst",
            "render_state": "failed",
        }
        replay_after_drift = await client.call("storage.raw_object.commit.v2", derived_drift)
        assert replay_after_drift["exact_replay"] is True
        assert replay_after_drift["receipt"] == committed["receipt"]

        representation_drift = {
            **raw,
            "session_id": str(uuid4()),
            "object_hash": "f" * 64,
            "payload_hash": "e" * 64,
            "compressed_hash": "f" * 64,
            "object_path": f"raw/v2/ff/{'f' * 64}.zst",
            "uncompressed_size": raw["uncompressed_size"] + 10,
            "compressed_size": raw["compressed_size"] + 10,
            "sealed_at": (now.replace(microsecond=1)).isoformat(),
        }
        replay_after_representation_drift = await client.call(
            "storage.raw_object.commit.v2",
            representation_drift,
        )
        assert replay_after_representation_drift["exact_replay"] is True
        assert replay_after_representation_drift["receipt"] == committed["receipt"]

        exists = await client.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [raw["envelope_id"], "1" * 64]},
        )
        assert exists["objects"][0]["receipt"] == committed["receipt"]
        assert exists["objects"][1]["receipt"] is None

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
                "receipt": committed["receipt"],
            },
            {
                "envelope_id": "f" * 64,
                "exists": False,
                "state": "missing",
                "object_hash": None,
                "commit_seq": None,
                "receipt": None,
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
            predecessor=epoch,
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
        "render_generations",
        "render_objects",
        "session_tombstones",
        "media_objects",
        "session_media_refs",
        "projector_state",
        "sessions",
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
