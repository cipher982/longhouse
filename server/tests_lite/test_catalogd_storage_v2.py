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
from zerg.embedding_space import EMBEDDING_PROJECTOR_ID
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
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
    provider: str = "codex",
) -> dict:
    record_hashes = tuple(hashlib.sha256(record).digest() for record in records)
    identity = EnvelopeIdentity(
        tenant_id="tenant-a",
        machine_id=machine_id,
        provider=provider,
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
        "provider": provider,
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
    provider: str = "codex",
) -> dict:
    source_epoch = source_epoch or UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    object_hash = hashlib.sha256(seed).hexdigest()
    first_key = json.dumps(
        [
            1_700_000_000_000_000 + position,
            "cinder",
            provider,
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
async def test_storage_telemetry_summary_is_bounded_and_accounts_objects(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        epoch = uuid4()
        session_id = uuid4()
        raw = _raw_params(epoch=epoch, session_id=session_id, start=0, end=6, records=(b"hello\n",), sealed_at=now)
        raw.update(
            render_state="ready",
            render_manifest=_render_manifest(uuid4(), source_epoch=epoch),
            projectors=["search-v2"],
        )
        await client.call("storage.raw_object.commit.v2", raw)

        summary = await client.call("storage.telemetry.summary.v2", {})

        assert summary["objects"] == {
            "raw": {"count": 1, "bytes": 3},
            "render": {"count": 1, "bytes": 80},
            "media": {"count": 0, "bytes": 0},
        }
        assert summary["projectors"] == [
            {
                "projector": "search-v2",
                "lagging": 1,
                "failed": 0,
                "claimed": 0,
                "oldest_lag_updated_at": summary["projectors"][0]["oldest_lag_updated_at"],
            }
        ]

        await client.call(
            "storage.session.delete.v2",
            {
                "session_id": str(session_id),
                "deletion_id": str(uuid4()),
                "reason": "telemetry_test",
                "deleted_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        retired = await client.call("storage.telemetry.summary.v2", {})
        assert retired["objects"]["raw"] == {"count": 0, "bytes": 0}
        assert retired["objects"]["render"] == {"count": 0, "bytes": 0}
        # Deletion intentionally leaves one lagging search-v2 cleanup job.
        assert retired["projectors"][0]["projector"] == "search-v2"
        assert retired["projectors"][0]["lagging"] == 1
    finally:
        await client.close()
        await daemon.close()


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
async def test_semantic_projection_repair_updates_legacy_catalog_aggregates_and_title(daemon_paths):
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
        await client.call("storage.raw_object.commit.v2", raw)

        repaired = await client.call(
            "storage.session.semantic_projection.repair.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "objects": [
                    {
                        "object_id": raw["render_manifest"]["object_id"],
                        "event_count": 1,
                        "user_messages": 1,
                        "assistant_messages": 0,
                        "tool_calls": 0,
                        "first_user_message_preview": "The real prompt",
                        "last_visible_text_preview": "The real prompt",
                    }
                ],
                "observed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )

        assert repaired["complete"] is True
        assert repaired["updated_object_count"] == 1
        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert session["session"]["semantic_projection_version"] == 1
        assert session["session"]["first_user_message_preview"] == "The real prompt"
        assert session["session"]["summary_title"] == "The real prompt"
        assert session["session"]["anchor_title"] is None
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_empty_render_object_is_repairable_and_can_complete_semantic_projection(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    generation_id = uuid4()
    empty_render = _render_manifest(generation_id, source_epoch=epoch)
    empty_render.update(
        event_count=0,
        first_order_key=None,
        last_order_key=None,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        first_user_message_preview=None,
        last_visible_text_preview=None,
        semantic_projection_version=0,
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        raw = _raw_params(epoch=epoch, session_id=session_id, start=0, end=1, records=(b"\n",), sealed_at=now)
        raw.update(render_state="ready", render_manifest=empty_render, projectors=["search-v2"])
        await client.call("storage.raw_object.commit.v2", raw)
        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})

        objects = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(generation_id),
                "snapshot_revision": int(session["commit_seq"]),
                "after_object_id": None,
                "limit": 100,
            },
        )
        assert objects["snapshot_object_count"] == 1
        assert objects["snapshot_event_count"] == 0
        assert len(objects["objects"]) == 1
        assert objects["objects"][0]["event_count"] == 0

        repaired = await client.call(
            "storage.session.semantic_projection.repair.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "objects": [
                    {
                        "object_id": empty_render["object_id"],
                        "event_count": 0,
                        "user_messages": 0,
                        "assistant_messages": 0,
                        "tool_calls": 0,
                        "first_user_message_preview": None,
                        "last_visible_text_preview": None,
                    }
                ],
                "observed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )

        assert repaired["complete"] is True
        assert repaired["updated_object_count"] == 1
        refreshed = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert refreshed["session"]["semantic_projection_version"] == 1
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_semantic_repair_preserves_an_unrelated_frozen_title(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    generation_id = uuid4()
    render = _render_manifest(generation_id, source_epoch=epoch)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        raw = _raw_params(epoch=epoch, session_id=session_id, start=0, end=1, records=(b"prompt\n",), sealed_at=now)
        raw.update(render_state="ready", render_manifest=render, projectors=["search-v2"])
        await client.call("storage.raw_object.commit.v2", raw)
        await client.call(
            "storage.session.title.complete.v2",
            {
                "session_id": str(session_id),
                "title": "Human title",
                "completed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )

        repaired = await client.call(
            "storage.session.semantic_projection.repair.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "objects": [
                    {
                        "object_id": render["object_id"],
                        "event_count": 1,
                        "user_messages": 1,
                        "assistant_messages": 0,
                        "tool_calls": 0,
                        "first_user_message_preview": "Build it",
                        "last_visible_text_preview": "Build it",
                    }
                ],
                "observed_at": (now + timedelta(seconds=2)).isoformat(),
            },
        )

        assert repaired["complete"] is True
        refreshed = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert refreshed["session"]["semantic_projection_version"] == 1
        assert refreshed["session"]["anchor_title"] == "Human title"
        assert refreshed["session"]["summary_title"] == "Human title"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_semantic_projection_repair_clears_control_only_fallback_title(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    generation_id = uuid4()
    render = _render_manifest(generation_id, source_epoch=epoch, provider="claude")
    render.update(
        user_messages=1,
        first_user_message_preview="Effort level settings",
        last_visible_text_preview="Effort level settings",
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        raw = _raw_params(
            epoch=epoch,
            session_id=session_id,
            start=0,
            end=1,
            records=(b"/effort\n",),
            sealed_at=now,
            provider="claude",
        )
        raw.update(render_state="ready", render_manifest=render, projectors=["search-v2"])
        await client.call("storage.raw_object.commit.v2", raw)
        await client.call(
            "storage.session.title.complete.v2",
            {
                "session_id": str(session_id),
                "title": "Effort controls",
                "completed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        before = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert before["session"]["summary_title"] == "Effort controls"

        repaired = await client.call(
            "storage.session.semantic_projection.repair.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "objects": [
                    {
                        "object_id": render["object_id"],
                        "event_count": 1,
                        "user_messages": 0,
                        "assistant_messages": 0,
                        "tool_calls": 0,
                        "first_user_message_preview": None,
                        "last_visible_text_preview": None,
                    }
                ],
                "observed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )

        assert repaired["complete"] is True
        after = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert after["session"]["first_user_message_preview"] is None
        assert after["session"]["summary_title"] is None
        assert after["session"]["anchor_title"] is None
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_revision_generation_drift_returns_conflict_instead_of_catalog_failure(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        existing_generation_id = uuid4()
        first = _raw_params(epoch=epoch, session_id=session_id, start=0, end=6, records=(b"hello\n",), sealed_at=now)
        first.update(render_state="ready", render_manifest=_render_manifest(existing_generation_id), projectors=["search-v2"])
        await client.call("storage.raw_object.commit.v2", first)

        requested_generation_id = uuid4()
        drifted = _raw_params(epoch=epoch, session_id=session_id, start=6, end=12, records=(b"world\n",), sealed_at=now)
        drifted.update(
            render_state="ready",
            render_manifest=_render_manifest(requested_generation_id, seed=b"second-render", position=6),
            projectors=["search-v2"],
        )
        with pytest.raises(CatalogRemoteError) as conflict:
            await client.call("storage.raw_object.commit.v2", drifted)
        assert conflict.value.code == "source_epoch_conflict"
        assert conflict.value.details == {
            "reason": "render_generation_revision_conflict",
            "existing_generation_id": str(existing_generation_id),
            "requested_generation_id": str(requested_generation_id),
            "parser_revision": drifted["render_manifest"]["parser_revision"],
            "ordering_revision": drifted["render_manifest"]["ordering_revision"],
        }
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
            provider_session_id="provider-before-reset",
        )

        await client.call("storage.raw_object.commit.v2", raw)
        after_reset = _raw_params(
            epoch=uuid4(),
            session_id=session_id,
            start=0,
            end=6,
            records=(b"after\n",),
            sealed_at=now + timedelta(seconds=3),
            opaque_source_id="history-after-reset.jsonl",
        )
        after_reset["session_facts"]["provider_session_id"] = "provider-after-reset"
        # Coarse provider clocks may give both sides the same activity time;
        # insertion order must still make the new conversation the resume head.
        after_reset["session_facts"]["last_activity_at"] = raw["session_facts"]["last_activity_at"]
        await client.call("storage.raw_object.commit.v2", after_reset)
        stored = await client.call("storage.session.read.v2", {"session_id": str(session_id)})

        assert stored["session"]["origin_kind"] == "console"
        assert stored["session"]["launch_actor"] == "user"
        assert stored["session"]["launch_surface"] == "ios"
        assert stored["session"]["ended_at"] is None
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        db.add(LiveUser(id=42, email="owner@example.com", is_active=True))
        db.commit()
    next_turn = CatalogStore(engine).enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 42,
            "message": "continue after reset",
            "client_request_id": "after-reset-turn",
            "created_at": now + timedelta(seconds=4),
        }
    )
    assert next_turn["turn"]["resume_provider_thread_id"] == "provider-after-reset"
    with Session(engine) as db:
        aliases = (
            db.query(LiveSessionThreadAlias)
            .filter(
                LiveSessionThreadAlias.thread_id == str(thread_id),
                LiveSessionThreadAlias.alias_kind == "provider_session_id",
            )
            .order_by(
                LiveSessionThreadAlias.last_seen_at.desc(),
                LiveSessionThreadAlias.first_seen_at.desc(),
                LiveSessionThreadAlias.id.desc(),
            )
            .all()
        )
        assert [alias.alias_value for alias in aliases] == [
            "provider-after-reset",
            "provider-before-reset",
        ]
    engine.dispose()


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
        first_manifest["first_user_message_preview"] = "[Image #1]\n\nWhy is OpenCode stuck on naming sessions and how do we fix it?"
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
async def test_claude_title_candidates_wait_for_semantic_repair(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    generation_id = uuid4()
    render = _render_manifest(generation_id, source_epoch=epoch, provider="claude")
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        raw = _raw_params(
            epoch=epoch,
            session_id=session_id,
            start=0,
            end=1,
            records=(b"provider command\n",),
            sealed_at=now,
            provider="claude",
        )
        raw.update(render_state="ready", render_manifest=render, projectors=["search-v2"])
        await client.call("storage.raw_object.commit.v2", raw)

        pending = await client.call("storage.session.title.candidates.v2", {"limit": 10})
        assert pending["sessions"] == []

        await client.call(
            "storage.session.semantic_projection.repair.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "objects": [
                    {
                        "object_id": render["object_id"],
                        "event_count": 1,
                        "user_messages": 1,
                        "assistant_messages": 0,
                        "tool_calls": 0,
                        "first_user_message_preview": "Build it",
                        "last_visible_text_preview": "Build it",
                    }
                ],
                "observed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        ready = await client.call("storage.session.title.candidates.v2", {"limit": 10})
        assert [row["session_id"] for row in ready["sessions"]] == [str(session_id)]
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
async def test_recall_projectors_claim_oldest_lag_before_hotter_revision(daemon_paths):
    """Live ingest must not starve the stable corpus behind a coverage gate."""

    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    # Make lexical UUID order oppose age order so the test catches a fallback
    # to session-id ordering in either projector.
    stable_session = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    hot_session = UUID("00000000-0000-4000-8000-000000000001")
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        stable = _raw_params(
            epoch=uuid4(),
            session_id=stable_session,
            start=0,
            end=6,
            records=(b"stable backlog\n",),
            sealed_at=now,
        )
        stable.update(
            render_state="ready",
            render_manifest=_render_manifest(uuid4(), seed=b"stable-backlog"),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        await client.call("storage.raw_object.commit.v2", stable)

        hot_epoch = uuid4()
        hot = _raw_params(
            epoch=hot_epoch,
            session_id=hot_session,
            start=0,
            end=6,
            records=(b"new live revision\n",),
            sealed_at=now + timedelta(seconds=1),
            opaque_source_id="hot-history.jsonl",
        )
        hot.update(
            render_state="ready",
            render_manifest=_render_manifest(
                uuid4(),
                seed=b"hot-revision",
                opaque_source_id="hot-history.jsonl",
                source_epoch=hot_epoch,
            ),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        await client.call("storage.raw_object.commit.v2", hot)

        claim = await client.call(
            "projector.state.claim.v2",
            {
                "projector": "search-v2",
                "worker_id": "search-worker",
                "claim_token": str(uuid4()),
                "now": (now + timedelta(seconds=2)).isoformat(),
                "lease_seconds": 60,
                "limit": 1,
            },
        )
        assert [row["session_id"] for row in claim["claimed"]] == [str(stable_session)]
        stable_revision = int(claim["claimed"][0]["claimed_revision"])
        await client.call(
            "projector.state.complete.v2",
            {
                "projector": "search-v2",
                "session_id": str(stable_session),
                "claim_token": claim["claimed"][0]["claim_token"],
                "completed_revision": stable_revision,
                "completed_at": (now + timedelta(seconds=2)).isoformat(),
            },
        )
        hot_claim = await client.call(
            "projector.state.claim.v2",
            {
                "projector": "search-v2",
                "worker_id": "search-worker",
                "claim_token": str(uuid4()),
                "now": (now + timedelta(seconds=3)).isoformat(),
                "lease_seconds": 60,
                "limit": 1,
            },
        )
        assert [row["session_id"] for row in hot_claim["claimed"]] == [str(hot_session)]
        await client.call(
            "projector.state.complete.v2",
            {
                "projector": "search-v2",
                "session_id": str(hot_session),
                "claim_token": hot_claim["claimed"][0]["claim_token"],
                "completed_revision": int(hot_claim["claimed"][0]["claimed_revision"]),
                "completed_at": (now + timedelta(seconds=3)).isoformat(),
            },
        )

        embedding_claim = await client.call(
            "projector.state.claim.v2",
            {
                "projector": EMBEDDING_PROJECTOR_ID,
                "worker_id": "embedding-worker",
                "claim_token": str(uuid4()),
                "now": (now + timedelta(seconds=4)).isoformat(),
                "lease_seconds": 60,
                "limit": 1,
            },
        )
        assert [row["session_id"] for row in embedding_claim["claimed"]] == [str(stable_session)]
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
async def test_semantic_repair_revision_keeps_next_render_object_page_visible(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    epoch = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
    session_id = uuid4()
    generation_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        first = _raw_params(epoch=epoch, session_id=session_id, start=0, end=6, records=(b"first\n",), sealed_at=now)
        first.update(
            render_state="ready",
            render_manifest=_render_manifest(generation_id, seed=b"first", position=0),
            projectors=["search-v2"],
        )
        await client.call("storage.raw_object.commit.v2", first)
        second = _raw_params(epoch=epoch, session_id=session_id, start=6, end=12, records=(b"second\n",), sealed_at=now)
        second.update(
            render_state="ready",
            render_manifest=_render_manifest(generation_id, seed=b"second", position=6),
            projectors=["search-v2"],
        )
        committed = await client.call("storage.raw_object.commit.v2", second)
        snapshot_revision = int(committed["receipt"]["commit_seq"])

        first_page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(generation_id),
                "snapshot_revision": snapshot_revision,
                "after_object_id": None,
                "limit": 1,
            },
        )
        first_object = first_page["objects"][0]
        second_object_id = (
            first["render_manifest"]["object_id"]
            if first_object["object_id"] == second["render_manifest"]["object_id"]
            else second["render_manifest"]["object_id"]
        )
        repaired = await client.call(
            "storage.session.semantic_projection.repair.v2",
            {
                "session_id": str(session_id),
                "owner_id": "42",
                "generation_id": str(generation_id),
                "objects": [
                    {
                        "object_id": first_object["object_id"],
                        "event_count": first_object["event_count"],
                        "user_messages": 1,
                        "assistant_messages": 0,
                        "tool_calls": 0,
                        "first_user_message_preview": "Repaired first",
                        "last_visible_text_preview": "Repaired first",
                    }
                ],
                "observed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        assert int(repaired["commit_seq"]) > snapshot_revision

        second_page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(generation_id),
                "snapshot_revision": int(repaired["commit_seq"]),
                "after_object_id": first_object["object_id"],
                "limit": 1,
            },
        )
        assert second_page["snapshot_object_count"] == 2
        assert [row["object_id"] for row in second_page["objects"]] == [second_object_id]
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
        table_names = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    engine.dispose()
    assert set(CatalogBase.metadata.tables).issubset(table_names)


def test_existing_v1_catalog_additively_creates_storage_v2_tables(daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        for table_name in CatalogBase.metadata.tables:
            connection.exec_driver_sql(f'DROP TABLE "{table_name}"')
        # Model a catalog from the preceding v2 build, before the additive
        # reducer marker and tables existed.
        connection.exec_driver_sql("UPDATE catalog_meta SET fact_reducer_generation = NULL")

    metadata = initialize_catalog_schema(engine)
    with engine.connect() as connection:
        table_names = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    engine.dispose()

    assert metadata.schema_version == CATALOG_SCHEMA_VERSION
    assert set(CatalogBase.metadata.tables).issubset(table_names)


@pytest.mark.asyncio
async def test_active_embedding_projector_state_becomes_claimable_on_render_completion(daemon_paths):
    """Regression guard for a real bug: the embedding projector never got a
    projector_state row at all, because every site that creates/advances one
    was hardcoded to the literal string "search-v2". claim.v2 itself is
    generic across projector names, so a test that only mocks a claim
    response proves nothing about this -- it has to go through a real
    CatalogDaemon and the actual render-generation-completion write path.
    """
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
        raw.update(render_state="ready", render_manifest=_render_manifest(generation_id), projectors=["search-v2", EMBEDDING_PROJECTOR_ID])
        committed = await client.call("storage.raw_object.commit.v2", raw)
        revision = committed["receipt"]["commit_seq"]

        search_token = str(uuid4())
        search_claim = await client.call(
            "projector.state.claim.v2",
            {
                "projector": "search-v2",
                "worker_id": "search-worker",
                "claim_token": search_token,
                "now": (now + timedelta(seconds=1)).isoformat(),
                "lease_seconds": 60,
                "limit": 10,
            },
        )
        assert search_claim["claimed"][0]["session_id"] == str(session_id)
        assert search_claim["claimed"][0]["claimed_revision"] == revision
        await client.call(
            "projector.state.complete.v2",
            {
                "projector": "search-v2",
                "session_id": str(session_id),
                "claim_token": search_token,
                "completed_revision": int(revision),
                "completed_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )

        embeddings_claim = await client.call(
            "projector.state.claim.v2",
            {
                "projector": EMBEDDING_PROJECTOR_ID,
                "worker_id": "embeddings-worker",
                "claim_token": str(uuid4()),
                "now": (now + timedelta(seconds=1)).isoformat(),
                "lease_seconds": 60,
                "limit": 10,
            },
        )
        assert embeddings_claim["claimed"], "the active embedding space must get its own claimable projector_state row"
        assert embeddings_claim["claimed"][0]["session_id"] == str(session_id)
        assert embeddings_claim["claimed"][0]["claimed_revision"] == revision

        # Model an existing catalog created before this projector identity was
        # introduced. Startup must synthesize the missing state or the bumped
        # projector will never see the historical corpus.
        engine = create_catalog_engine(database_path)
        with engine.begin() as connection:
            # Session metadata can advance after the render/search revision,
            # including a transition back to pending for semantic repair. A
            # newly introduced embedding projector must still mirror the
            # already-published search ledger and inherit its revision, not
            # this unrelated catalog commit sequence.
            connection.exec_driver_sql(
                "UPDATE sessions SET commit_seq = commit_seq + 10, render_state = 'pending' WHERE session_id = ?",
                (str(session_id),),
            )
            connection.exec_driver_sql(
                "DELETE FROM projector_state WHERE projector = ? AND session_id = ?",
                (EMBEDDING_PROJECTOR_ID, str(session_id)),
            )
        assert CatalogStore(engine).ensure_known_projector_states()["inserted"] == 1
        with engine.connect() as connection:
            restored = connection.exec_driver_sql(
                "SELECT desired_revision, completed_revision, status FROM projector_state WHERE projector = ? AND session_id = ?",
                (EMBEDDING_PROJECTOR_ID, str(session_id)),
            ).one()
        engine.dispose()
        assert tuple(restored) == (int(revision), 0, "idle")

        engine = create_catalog_engine(database_path)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE projector_state SET desired_revision = ?, claimed_revision = ?, claim_token = ?, "
                "worker_id = ?, status = 'failed', failure_count = 3, last_error_code = 'stale', "
                "last_error_message = 'stale revision', retry_at = ? "
                "WHERE projector = ? AND session_id = ?",
                (
                    int(revision) + 10,
                    int(revision) + 10,
                    str(uuid4()),
                    "stale-worker",
                    now + timedelta(minutes=5),
                    EMBEDDING_PROJECTOR_ID,
                    str(session_id),
                ),
            )
        repaired = CatalogStore(engine).ensure_known_projector_states()
        assert repaired["aligned_embeddings"] == 1
        with engine.connect() as connection:
            aligned = connection.exec_driver_sql(
                "SELECT desired_revision, claimed_revision, claim_token, worker_id, status, "
                "failure_count, last_error_code, last_error_message, retry_at "
                "FROM projector_state WHERE projector = ? AND session_id = ?",
                (EMBEDDING_PROJECTOR_ID, str(session_id)),
            ).one()
        engine.dispose()
        assert tuple(aligned) == (int(revision), None, None, None, "idle", 0, None, None, None)
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_source_epoch_replacement_advances_retired_projectors(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    old_epoch = uuid4()
    new_epoch = uuid4()
    old_session = uuid4()
    new_session = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        old_raw = _raw_params(
            epoch=old_epoch,
            session_id=old_session,
            start=0,
            end=6,
            records=(b"old\n",),
            sealed_at=now,
        )
        old_raw.update(
            render_state="ready",
            render_manifest=_render_manifest(uuid4(), seed=b"old-render", source_epoch=old_epoch),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        old_commit = await client.call("storage.raw_object.commit.v2", old_raw)

        replacement = _raw_params(
            epoch=new_epoch,
            predecessor=old_epoch,
            session_id=new_session,
            start=6,
            end=7,
            records=(b"new\n",),
            sealed_at=now + timedelta(seconds=1),
        )
        replacement.update(
            render_state="ready",
            render_manifest=_render_manifest(uuid4(), seed=b"new-render", source_epoch=new_epoch, position=6),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        replaced = await client.call("storage.raw_object.commit.v2", replacement)
        assert int(replaced["receipt"]["commit_seq"]) > int(old_commit["receipt"]["commit_seq"])

        engine = create_catalog_engine(database_path)
        with engine.connect() as connection:
            retired = connection.exec_driver_sql(
                "SELECT render_state, commit_seq FROM sessions WHERE session_id = ?",
                (str(old_session),),
            ).one()
            states = connection.exec_driver_sql(
                "SELECT projector, desired_revision, claim_token, status FROM projector_state WHERE session_id = ? ORDER BY projector",
                (str(old_session),),
            ).all()
        assert retired[0] == "retired"
        assert all(row[1] == retired[1] and row[2] is None and row[3] == "idle" for row in states)

        # Startup repair must also heal sessions retired before this invariant
        # was introduced, including stale claims stuck on the old snapshot.
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE projector_state SET desired_revision = ?, claimed_revision = ?, claim_token = ?, "
                "worker_id = ?, claim_expires_at = ?, status = ?, retry_at = ? WHERE session_id = ?",
                (
                    int(old_commit["receipt"]["commit_seq"]),
                    int(old_commit["receipt"]["commit_seq"]),
                    "stale-claim",
                    "dead-worker",
                    now + timedelta(minutes=5),
                    "retry",
                    now + timedelta(minutes=5),
                    str(old_session),
                ),
            )
        assert CatalogStore(engine).ensure_known_projector_states()["advanced_retired"] == 2
        with engine.connect() as connection:
            repaired_states = connection.exec_driver_sql(
                "SELECT desired_revision, claimed_revision, claim_token, worker_id, claim_expires_at, status, retry_at "
                "FROM projector_state WHERE session_id = ?",
                (str(old_session),),
            ).all()
        engine.dispose()
        assert all(
            row[0] == retired[1]
            and row[1] is None
            and row[2] is None
            and row[3] is None
            and row[4] is None
            and row[5] == "idle"
            and row[6] is None
            for row in repaired_states
        )

        page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(old_session),
                "generation_id": None,
                "snapshot_revision": int(retired[1]),
                "after_object_id": None,
                "limit": 100,
            },
        )
        assert page["found"] is True
        assert page["retired"] is True
        assert page["objects"] == []
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_relinked_legacy_reconciliation_requires_duplicate_proof_and_retires_projection(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    old_session, canonical_session = uuid4(), uuid4()
    legacy_epoch, native_epoch, replacement_epoch = uuid4(), uuid4(), uuid4()
    generation_id = uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        legacy = _raw_params(
            epoch=legacy_epoch,
            session_id=old_session,
            start=0,
            end=6,
            records=(b"legacy\n",),
            sealed_at=now,
            opaque_source_id="legacy-source-lines",
        )
        legacy["provenance_kind"] = "legacy_source_lines"
        await client.call("storage.raw_object.commit.v2", legacy)

        native = _raw_params(
            epoch=native_epoch,
            session_id=old_session,
            start=0,
            end=6,
            records=(b"native\n",),
            sealed_at=now + timedelta(seconds=1),
        )
        native.update(
            render_state="ready",
            render_manifest=_render_manifest(generation_id, source_epoch=native_epoch),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        await client.call("storage.raw_object.commit.v2", native)

        replacement = _raw_params(
            epoch=replacement_epoch,
            predecessor=native_epoch,
            session_id=canonical_session,
            start=0,
            end=6,
            records=(b"native\n",),
            sealed_at=now + timedelta(seconds=2),
        )
        replacement.update(
            render_state="ready",
            render_manifest=_render_manifest(uuid4(), seed=b"replacement-render", source_epoch=replacement_epoch),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        await client.call("storage.raw_object.commit.v2", replacement)

        stale = await client.call("storage.session.read.v2", {"session_id": str(old_session)})
        assert stale["session"]["render_state"] == "ready"
        assert stale["session"]["current_render_generation"] == str(generation_id)

        repaired = await client.call(
            "storage.session.relinked_legacy.reconcile.v2",
            {"session_id": str(old_session), "observed_at": (now + timedelta(seconds=3)).isoformat()},
        )
        assert repaired["changed"] is True
        assert repaired["preserved_raw_objects"] == 1
        assert repaired["replacement_proofs"][0]["replacement_session_id"] == str(canonical_session)

        retired = await client.call("storage.session.read.v2", {"session_id": str(old_session)})
        assert retired["session"]["raw_state"] == "durable"
        assert retired["session"]["render_state"] == "retired"
        assert retired["session"]["hidden_from_default_timeline"] is True
        page = await client.call(
            "storage.session.render_objects.list.v2",
            {
                "session_id": str(old_session),
                "generation_id": None,
                "snapshot_revision": int(repaired["commit_seq"]),
                "after_object_id": None,
                "limit": 10,
            },
        )
        assert page["retired"] is True

        replay = await client.call(
            "storage.session.relinked_legacy.reconcile.v2",
            {"session_id": str(old_session), "observed_at": (now + timedelta(seconds=4)).isoformat()},
        )
        assert replay["changed"] is False
        assert replay["already_retired"] is True

        with pytest.raises(CatalogRemoteError) as current_conflict:
            await client.call(
                "storage.session.relinked_legacy.reconcile.v2",
                {"session_id": str(canonical_session), "observed_at": (now + timedelta(seconds=4)).isoformat()},
            )
        assert current_conflict.value.code == "conflict"
        assert current_conflict.value.details == {"reason": "current_generation_not_retired"}
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_restore_generation_requires_retired_current_and_durable_target(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id, replacement_session = uuid4(), uuid4()
    legacy_epoch, native_epoch, replacement_epoch = uuid4(), uuid4(), uuid4()
    legacy_generation, native_generation = uuid4(), uuid4()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        legacy = _raw_params(
            epoch=legacy_epoch,
            session_id=session_id,
            start=0,
            end=6,
            records=(b"legacy\n",),
            sealed_at=now,
            opaque_source_id="legacy-source-lines",
        )
        legacy.update(
            provenance_kind="legacy_source_lines",
            render_state="ready",
            render_manifest=_render_manifest(
                legacy_generation,
                seed=b"legacy-render",
                opaque_source_id="legacy-source-lines",
                source_epoch=legacy_epoch,
            ),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        legacy["render_manifest"]["parser_revision"] = "legacy-normalized-v1"
        await client.call("storage.raw_object.commit.v2", legacy)

        native = _raw_params(
            epoch=native_epoch,
            session_id=session_id,
            start=0,
            end=6,
            records=(b"native\n",),
            sealed_at=now + timedelta(seconds=1),
        )
        native.update(
            render_state="ready",
            render_manifest=_render_manifest(native_generation, seed=b"native-render", source_epoch=native_epoch),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        await client.call("storage.raw_object.commit.v2", native)

        replacement = _raw_params(
            epoch=replacement_epoch,
            predecessor=native_epoch,
            session_id=replacement_session,
            start=0,
            end=6,
            records=(b"native\n",),
            sealed_at=now + timedelta(seconds=2),
        )
        replacement.update(
            render_state="ready",
            render_manifest=_render_manifest(uuid4(), seed=b"replacement-render", source_epoch=replacement_epoch),
            projectors=["search-v2", EMBEDDING_PROJECTOR_ID],
        )
        await client.call("storage.raw_object.commit.v2", replacement)

        restored = await client.call(
            "storage.session.render_generation.restore.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(legacy_generation),
                "observed_at": (now + timedelta(seconds=3)).isoformat(),
            },
        )
        assert restored["changed"] is True
        assert restored["object_count"] == 1
        assert restored["event_count"] == 1

        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert session["session"]["current_render_generation"] == str(legacy_generation)
        assert session["session"]["render_state"] == "ready"
        replay = await client.call(
            "storage.session.render_generation.restore.v2",
            {
                "session_id": str(session_id),
                "generation_id": str(legacy_generation),
                "observed_at": (now + timedelta(seconds=4)).isoformat(),
            },
        )
        assert replay["changed"] is False
        assert replay["already_current"] is True

        with pytest.raises(CatalogRemoteError) as retired_target:
            await client.call(
                "storage.session.render_generation.restore.v2",
                {
                    "session_id": str(session_id),
                    "generation_id": str(native_generation),
                    "observed_at": (now + timedelta(seconds=4)).isoformat(),
                },
            )
        assert retired_target.value.code == "conflict"
        assert retired_target.value.details == {"reason": "current_generation_still_durable"}
    finally:
        await client.close()
        await daemon.close()
