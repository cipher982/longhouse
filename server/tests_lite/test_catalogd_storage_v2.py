from __future__ import annotations

import hashlib
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import CatalogBase
from zerg.catalogd.models import RawObject as LiveRawObject
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.models import SourceEpoch as LiveSourceEpoch
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveTimelineCard
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
    opaque_source_id: str = "history.jsonl",
    machine_id: str = "cinder",
) -> dict:
    record_hashes = tuple(hashlib.sha256(record).digest() for record in records)
    identity = EnvelopeIdentity(
        tenant_id="tenant-a",
        machine_id=machine_id,
        provider="codex",
        opaque_source_id=opaque_source_id,
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
        "machine_id": machine_id,
        "provider": "codex",
        "opaque_source_id": opaque_source_id,
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
        "projectors": ["render-v2"],
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


def _render_manifest(
    generation_id: UUID,
    *,
    seed: bytes = b"render-object",
    position: int = 0,
    opaque_source_id: str = "history.jsonl",
    source_epoch: UUID | None = None,
) -> dict:
    source_epoch = source_epoch or UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    object_hash = hashlib.sha256(seed).hexdigest()
    first_key = json.dumps(
        [
            1_700_000_000_000_000 + position,
            "cinder",
            "codex",
            opaque_source_id,
            str(source_epoch),
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
async def test_first_durable_content_reveals_hidden_console_shell(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    CatalogStore(engine).create_console_session(
        data={
            "session_id": str(session_id),
            "thread_id": str(uuid4()),
            "owner_id": 42,
            "provider": "codex",
            "device_id": "cinder",
            "cwd": "/workspace/longhouse",
            "project": "longhouse",
            "provider_config": {},
            "started_at": now,
        }
    )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        epoch = uuid4()
        raw = _raw_params(epoch=epoch, session_id=session_id, start=0, end=10, records=(b"user",), sealed_at=now)
        raw.update(
            render_state="ready",
            render_manifest=_render_manifest(uuid4(), source_epoch=epoch),
            projectors=["search-v2"],
        )
        await client.call("storage.raw_object.commit.v2", raw)
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        assert db.get(StorageSession, str(session_id)).hidden_from_default_timeline == 0
        assert db.get(LiveSessionCatalog, str(session_id)).hidden_from_default_timeline == 0
        assert db.get(LiveTimelineCard, str(session_id)).hidden_from_default_timeline == 0
    engine.dispose()


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
        raw.update(render_state="ready", render_manifest=_render_manifest(generation_id), projectors=["search-v2"])
        committed = await client.call("storage.raw_object.commit.v2", raw)
        assert committed["receipt"]["render_state"] == "ready"
        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert session["session"]["current_render_generation"] == str(generation_id)
        assert session["session"]["user_messages"] == 1
        assert session["session"]["first_user_message_preview"] == "Build it"
        assert session["session"]["summary_title"] == "Build it"
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
                "before_order_key": None,
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
                "before_order_key": None,
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
                "before_order_key": None,
                "limit": 100,
            },
        )
        assert stale["stale_generation"] is True
        assert stale["current_generation_id"] == str(generation_id)
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_console_provenance_survives_first_archived_provider_transcript(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    thread_id = uuid4()
    epoch = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        await client.call(
            "session.console.create.v2",
            {
                "session": {
                    "session_id": str(session_id),
                    "thread_id": str(thread_id),
                    "owner_id": 42,
                    "provider": "codex",
                    "device_id": "cinder",
                    "cwd": "/workspace/longhouse",
                    "project": "longhouse",
                    "launch_surface": "ios",
                    "started_at": now.isoformat(),
                }
            },
        )
        raw = _raw_params(
            epoch=epoch,
            session_id=session_id,
            start=0,
            end=6,
            records=(b"hello\n",),
            sealed_at=now + timedelta(seconds=1),
        )
        raw["session_facts"].update(
            origin_kind=None,
            launch_actor=None,
            launch_surface=None,
            ended_at=(now + timedelta(seconds=2)).isoformat(),
        )

        await client.call("storage.raw_object.commit.v2", raw)
        stored = await client.call("storage.session.read.v2", {"session_id": str(session_id)})

        assert stored["session"]["origin_kind"] == "console"
        assert stored["session"]["launch_actor"] == "user"
        assert stored["session"]["launch_surface"] == "ios"
        assert stored["session"]["ended_at"] is None
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_storage_title_fallback_is_immediate_and_ai_completion_is_write_once(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = uuid4()
    session_id = uuid4()
    generation_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        first = _raw_params(epoch=epoch, session_id=session_id, start=0, end=6, records=(b"first\n",), sealed_at=now)
        first_manifest = _render_manifest(generation_id)
        first_manifest["first_user_message_preview"] = (
            "[Image #1]\n\nWhy is OpenCode stuck on naming sessions and how do we fix it?"
        )
        first.update(render_state="ready", render_manifest=first_manifest)
        await client.call("storage.raw_object.commit.v2", first)

        stored = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert stored["session"]["summary_title"] == "Why is OpenCode stuck on naming…"
        assert stored["session"]["anchor_title"] is None

        candidates = await client.call("storage.session.title.candidates.v2", {"limit": 10})
        assert [row["session_id"] for row in candidates["sessions"]] == [str(session_id)]

        exempted = await client.call(
            "storage.session.title.fail.v2",
            {
                "session_id": str(session_id),
                "reason": "no_meaningful_user_text",
                "failed_at": now.isoformat(),
            },
        )
        assert exempted["changed"] is True
        assert exempted["retry_at"] is None

        completed = await client.call(
            "storage.session.title.complete.v2",
            {"session_id": str(session_id), "title": "Repair OpenCode Session Naming", "completed_at": now.isoformat()},
        )
        assert completed["changed"] is True

        second = _raw_params(epoch=epoch, session_id=session_id, start=6, end=13, records=(b"second\n",), sealed_at=now)
        second_manifest = _render_manifest(generation_id, seed=b"second-render", position=6)
        second_manifest["first_user_message_preview"] = "A later message must not rename the session"
        second.update(render_state="ready", render_manifest=second_manifest)
        await client.call("storage.raw_object.commit.v2", second)

        stored = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert stored["session"]["anchor_title"] == "Repair OpenCode Session Naming"
        assert stored["session"]["summary_title"] == "Repair OpenCode Session Naming"

        replay = await client.call(
            "storage.session.title.complete.v2",
            {"session_id": str(session_id), "title": "Wrong Later Title", "completed_at": now.isoformat()},
        )
        assert replay["changed"] is False
        stored = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert stored["session"]["anchor_title"] == "Repair OpenCode Session Naming"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_storage_session_delete_fences_replay_retires_manifests_and_queues_search_cleanup(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = uuid4()
    session_id = uuid4()
    generation_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        raw = _raw_params(epoch=epoch, session_id=session_id, start=0, end=6, records=(b"hello\n",), sealed_at=now)
        raw.update(render_state="ready", render_manifest=_render_manifest(generation_id), projectors=["search-v2"])
        committed = await client.call("storage.raw_object.commit.v2", raw)
        deletion_id = str(uuid4())
        deleted = await client.call(
            "storage.session.delete.v2",
            {
                "session_id": str(session_id),
                "deletion_id": deletion_id,
                "reason": "user_requested",
                "deleted_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        assert deleted["changed"] is True
        assert deleted["retired_raw_objects"] == 1
        assert deleted["retired_render_objects"] == 1
        replay = await client.call(
            "storage.session.delete.v2",
            {
                "session_id": str(session_id),
                "deletion_id": deletion_id,
                "reason": "user_requested",
                "deleted_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        assert replay["changed"] is False and replay["exact_replay"] is True

        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert session["deleted"] is True
        existence = await client.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [committed["receipt"]["envelope_id"]]},
        )
        assert existence["objects"][0]["state"] == "deleted"
        cleanup_claim = await client.call(
            "projector.state.claim.v2",
            {
                "projector": "search-v2",
                "worker_id": "search-worker",
                "claim_token": str(uuid4()),
                "now": (now + timedelta(seconds=2)).isoformat(),
                "lease_seconds": 60,
                "limit": 10,
            },
        )
        assert cleanup_claim["claimed"][0]["session_id"] == str(session_id)
        assert cleanup_claim["claimed"][0]["claimed_revision"] == deleted["deletion_revision"]

        with pytest.raises(CatalogRemoteError) as resurrection:
            await client.call("storage.raw_object.commit.v2", raw)
        assert resurrection.value.code == "session_deleted"
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
        first.update(
            render_state="ready",
            render_manifest=_render_manifest(generation_id, seed=b"first", position=0),
            projectors=["search-v2"],
        )
        first_commit = await client.call("storage.raw_object.commit.v2", first)
        first_revision = first_commit["receipt"]["commit_seq"]

        second = _raw_params(epoch=epoch, session_id=session_id, start=6, end=12, records=(b"world\n",), sealed_at=now)
        second.update(
            render_state="ready",
            render_manifest=_render_manifest(generation_id, seed=b"second", position=6),
            projectors=["search-v2"],
        )
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
async def test_source_epoch_replacement_retires_only_superseded_membership(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    old_epoch = uuid4()
    side_epoch = uuid4()
    replacement_epoch = uuid4()
    session_id = uuid4()
    generation_id = uuid4()
    missing_hash = "d" * 64
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        old = _raw_params(
            epoch=old_epoch,
            session_id=session_id,
            start=0,
            end=4,
            records=(b"old\n",),
            sealed_at=now,
        )
        old.update(
            render_state="ready",
            render_manifest=_render_manifest(generation_id, seed=b"old", source_epoch=old_epoch),
            projectors=["search-v2"],
            media_refs=[
                {
                    "media_hash": missing_hash,
                    "source_position": 0,
                    "ref_key": "missing:0",
                    "availability": "missing",
                }
            ],
        )
        old_commit = await client.call("storage.raw_object.commit.v2", old)

        side = _raw_params(
            epoch=side_epoch,
            session_id=session_id,
            start=0,
            end=5,
            records=(b"side\n",),
            sealed_at=now,
            opaque_source_id="side.jsonl",
        )
        side.update(
            render_state="ready",
            render_manifest=_render_manifest(
                generation_id,
                seed=b"side",
                opaque_source_id="side.jsonl",
                source_epoch=side_epoch,
            ),
            projectors=["search-v2"],
        )
        await client.call("storage.raw_object.commit.v2", side)

        replacement = _raw_params(
            epoch=replacement_epoch,
            predecessor=old_epoch,
            session_id=session_id,
            start=0,
            end=4,
            records=(b"new\n",),
            sealed_at=now + timedelta(seconds=1),
        )
        replacement.update(
            render_state="ready",
            render_manifest=_render_manifest(generation_id, seed=b"new", source_epoch=replacement_epoch),
            projectors=["search-v2"],
        )
        replacement_commit = await client.call("storage.raw_object.commit.v2", replacement)

        historical_page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(generation_id),
                "snapshot_revision": int(old_commit["receipt"]["commit_seq"]),
                "after_object_id": None,
                "limit": 100,
            },
        )
        assert [row["source_envelope_id"] for row in historical_page["objects"]] == [old["envelope_id"]]

        raw_manifest = await client.call(
            "storage.session.raw_manifest.v2",
            {"session_id": str(session_id), "owner_id": "42", "after_source_key": None, "limit": 100},
        )
        assert {row["envelope_id"] for row in raw_manifest["objects"]} == {
            side["envelope_id"],
            replacement["envelope_id"],
        }
        render_page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(generation_id),
                "snapshot_revision": int(replacement_commit["receipt"]["commit_seq"]),
                "after_object_id": None,
                "limit": 100,
            },
        )
        assert render_page["snapshot_object_count"] == 2
        assert render_page["snapshot_event_count"] == 2
        assert {row["source_envelope_id"] for row in render_page["objects"]} == {
            side["envelope_id"],
            replacement["envelope_id"],
        }
        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert session["session"]["user_messages"] == 2
        assert session["session"]["media_state"] == "complete"
        assert session["session"]["missing_media_hashes"] == []
        media = await client.call(
            "storage.media.read.v2",
            {"media_hash": missing_hash, "session_id": str(session_id), "limit": 10},
        )
        assert media["refs"][0]["state"] == "retired"
        with pytest.raises(CatalogRemoteError) as stale_retry:
            await client.call("storage.raw_object.commit.v2", old)
        assert stale_retry.value.code == "source_epoch_conflict"
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
async def test_source_epoch_rebind_moves_visibility_to_managed_session(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    parsed_session_id = uuid4()
    managed_session_id = uuid4()
    parsed_epoch = uuid4()
    managed_epoch = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        parsed = _raw_params(
            epoch=parsed_epoch,
            session_id=parsed_session_id,
            start=0,
            end=4,
            records=(b"old\n",),
            sealed_at=now,
        )
        await client.call("storage.raw_object.commit.v2", parsed)

        managed = _raw_params(
            epoch=managed_epoch,
            predecessor=parsed_epoch,
            session_id=managed_session_id,
            start=0,
            end=4,
            records=(b"old\n",),
            sealed_at=now + timedelta(seconds=1),
        )
        await client.call("storage.raw_object.commit.v2", managed)

        timeline = await client.call(
            "storage.session.timeline.list.v2",
            {
                "owner_id": "42",
                "before_last_activity_at": None,
                "before_session_id": None,
                "project": None,
                "provider": None,
                "include_test": False,
                "limit": 100,
            },
        )
        assert [row["session_id"] for row in timeline["sessions"]] == [str(managed_session_id)]

        parsed_manifest = await client.call(
            "storage.session.raw_manifest.v2",
            {"session_id": str(parsed_session_id), "owner_id": "42", "after_source_key": None, "limit": 100},
        )
        managed_manifest = await client.call(
            "storage.session.raw_manifest.v2",
            {"session_id": str(managed_session_id), "owner_id": "42", "after_source_key": None, "limit": 100},
        )
        assert parsed_manifest["objects"] == []
        assert [row["envelope_id"] for row in managed_manifest["objects"]] == [managed["envelope_id"]]
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_session_accepts_native_source_after_machine_rename(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        before_rename = _raw_params(
            epoch=uuid4(),
            session_id=session_id,
            start=0,
            end=4,
            records=(b"old\n",),
            sealed_at=now,
            opaque_source_id="old-machine.jsonl",
            machine_id="shipper-laptop",
        )
        after_rename = _raw_params(
            epoch=uuid4(),
            session_id=session_id,
            start=0,
            end=4,
            records=(b"new\n",),
            sealed_at=now + timedelta(seconds=1),
            opaque_source_id="current-machine.jsonl",
            machine_id="cinder",
        )

        await client.call("storage.raw_object.commit.v2", before_rename)
        await client.call("storage.raw_object.commit.v2", after_rename)

        manifest = await client.call(
            "storage.session.raw_manifest.v2",
            {"session_id": str(session_id), "owner_id": "42", "after_source_key": None, "limit": 100},
        )
        assert {row["machine_id"] for row in manifest["objects"]} == {"shipper-laptop", "cinder"}
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
        assert projector_lag["lag_count"] == 1
        assert projector_lag["indexed_through"] == str(int(committed["receipt"]["commit_seq"]) - 1)

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

        contiguous = _raw_params(
            epoch=next_epoch,
            predecessor=epoch,
            session_id=session_id,
            start=6,
            end=7,
            records=(b"z",),
            sealed_at=now,
        )
        assert (await client.call("storage.raw_object.commit.v2", contiguous))["receipt"]["commit_seq"] == "4"
        high_start = (1 << 64) - 2
        high_raw = _raw_params(
            epoch=next_epoch,
            predecessor=epoch,
            session_id=session_id,
            start=high_start,
            end=high_start + 1,
            records=(b"x",),
            sealed_at=now,
        )
        with pytest.raises(CatalogRemoteError) as gap_error:
            await client.call("storage.raw_object.commit.v2", high_raw)
        assert gap_error.value.code == "source_epoch_conflict"
        assert gap_error.value.details == {
            "reason": "range_gap",
            "accepted_through": "7",
            "requested_range_start": str(high_start),
            "requested_range_end": str(high_start + 1),
            "overlapping_envelope_ids": [],
        }
        out_of_order = _raw_params(
            epoch=next_epoch,
            session_id=session_id,
            start=6,
            end=7,
            records=(b"o",),
            sealed_at=now,
        )
        with pytest.raises(CatalogRemoteError) as out_of_order_error:
            await client.call("storage.raw_object.commit.v2", out_of_order)
        assert out_of_order_error.value.code == "source_epoch_conflict"

        await client.close()
        await daemon.close()
        engine = create_catalog_engine(database_path)
        with engine.begin() as connection:
            connection.execute(
                LiveSourceEpoch.__table__.update()
                .where(LiveSourceEpoch.__table__.c.source_epoch == str(next_epoch))
                .values(accepted_through=f"{999:020d}")
            )
        engine.dispose()
        daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
        await daemon.start()
        client = CatalogClient(socket_path)
        high_manifest = await client.call(
            "storage.source_epoch.manifest.v2",
            {"source_epoch": str(next_epoch), "after_position": 0, "limit": 100},
        )
        assert high_manifest["objects"][0]["range_start"] == "6"
        assert high_manifest["source_epoch"]["accepted_through"] == "999"
        reclaimed = _raw_params(
            epoch=next_epoch,
            predecessor=epoch,
            session_id=session_id,
            start=7,
            end=8,
            records=(b"y",),
            sealed_at=now,
        )
        assert (await client.call("storage.raw_object.commit.v2", reclaimed))["created"] is True
        reclaimed_manifest = await client.call(
            "storage.source_epoch.manifest.v2",
            {"source_epoch": str(next_epoch), "after_position": 0, "limit": 100},
        )
        assert reclaimed_manifest["source_epoch"]["accepted_through"] == "8"
        assert (await client.call("ping.v2"))["commit_seq"] == "5"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_storage_health_reports_owner_freshness_without_legacy_database(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    raw = _raw_params(
        epoch=uuid4(),
        session_id=session_id,
        start=0,
        end=6,
        records=(b"hello\n",),
        sealed_at=now,
    )
    raw["media_refs"] = [
        {
            "media_hash": hashlib.sha256(b"missing").hexdigest(),
            "source_position": 0,
            "ref_key": "missing:0",
            "availability": "missing",
        }
    ]
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            LiveHeartbeatStamp.__table__.insert().values(
                device_id="cinder",
                received_at=now,
                is_offline=0,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        await client.call("storage.raw_object.commit.v2", raw)
        health = await client.call("storage.health.v2", {"owner_id": "42"})
        assert health["session_count"] == 1
        assert health["last_session_at"] == now.isoformat()
        assert health["last_heartbeat_at"] == now.isoformat()
        assert health["media_repair_refs"] == 1
        assert health["media_repair_bytes"] == 0

        other_owner = await client.call("storage.health.v2", {"owner_id": "7"})
        assert other_owner["session_count"] == 0
        assert other_owner["last_session_at"] is None
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_raw_manifest_distinguishes_session_tombstone_from_retired_epoch(daemon_paths):
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
        assert retired.value.code == "source_epoch_conflict"
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

    assert metadata.schema_version == CATALOG_SCHEMA_VERSION
    assert set(CatalogBase.metadata.tables).issubset(table_names)
