from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.server import CatalogDaemon
from zerg.searchd.server import SearchDaemon
from zerg.services.search_v2_projector import SearchV2Projector
from zerg.storage_v2.raw_objects import RawObjectCorruptError
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import read_raw_object
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object


class _RenderReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def read(self, object_path: str, object_hash: str, *, lane: str):
        assert lane == "background"
        return read_render_object(self.root, object_path, expected_object_hash=object_hash)


def _socket_root(prefix: str) -> Path:
    root = Path("/tmp") / f"{prefix}-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    return root


def _render_manifest(sealed, generation_id: UUID) -> dict[str, object]:
    return {
        "generation_id": str(generation_id),
        "parser_revision": "destructive-proof-v1",
        "ordering_revision": "semantic-order-v2",
        "object_id": sealed.object_id,
        "object_hash": sealed.object_hash,
        "payload_hash": sealed.payload_hash,
        "object_path": sealed.object_path,
        "uncompressed_size": sealed.uncompressed_size,
        "compressed_size": sealed.compressed_size,
        "event_count": sealed.event_count,
        "first_order_key": sealed.first_order_key,
        "last_order_key": sealed.last_order_key,
        "user_messages": sealed.user_messages,
        "assistant_messages": sealed.assistant_messages,
        "tool_calls": sealed.tool_calls,
        "first_user_message_preview": sealed.first_user_message_preview,
        "last_visible_text_preview": sealed.last_visible_text_preview,
    }


def _build_storage_commit(
    object_root: Path,
    *,
    session_id: UUID,
    text: str,
    now: datetime,
    source_epoch: UUID | None = None,
) -> tuple[dict[str, object], object, object]:
    source_epoch = source_epoch or uuid4()
    generation_id = uuid4()
    opaque_source_id = f"machine-agent/{source_epoch}.jsonl"
    raw_spec = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="codex",
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        range_kind="record_ordinal",
        range_start=0,
        range_end=1,
        records=(RawRecord(source_position=0, data=text.encode()),),
    )
    sealed_raw = seal_raw_object(object_root, raw_spec)
    render_spec = RenderObjectSpec(
        session_id=session_id,
        render_generation=generation_id,
        parser_revision="destructive-proof-v1",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="codex",
        opaque_source_id=opaque_source_id,
        source_epoch=source_epoch,
        source_envelope_id=sealed_raw.envelope_id,
        records=(
            RenderRecord(
                event_id=f"event-{sealed_raw.envelope_id[:16]}",
                order_time_us=int(now.timestamp() * 1_000_000),
                source_position=0,
                event_subordinal=0,
                role="user",
                content_text=text,
                thread_id=str(uuid4()),
                branch_kind="head",
                raw_record_ordinal=0,
            ),
        ),
    )
    sealed_render = seal_render_object(object_root, render_spec)
    params = {
        "protocol_version": 2,
        "tenant_id": "tenant-a",
        "owner_id": "42",
        "session_id": str(session_id),
        "machine_id": "cinder",
        "provider": "codex",
        "opaque_source_id": opaque_source_id,
        "source_epoch": str(source_epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": now.isoformat(),
        "range_kind": "record_ordinal",
        "range_start": 0,
        "range_end": 1,
        "record_hashes": list(sealed_raw.record_hashes),
        "envelope_id": sealed_raw.envelope_id,
        "object_hash": sealed_raw.object_hash,
        "payload_hash": sealed_raw.payload_hash,
        "compressed_hash": sealed_raw.compressed_hash,
        "object_path": sealed_raw.object_path,
        "uncompressed_size": sealed_raw.uncompressed_size,
        "compressed_size": sealed_raw.compressed_size,
        "provenance_kind": "native",
        "render_state": "ready",
        "media_refs": [],
        "projectors": ["search-v2"],
        "render_manifest": _render_manifest(sealed_render, generation_id),
        "session_facts": {
            "environment": "production",
            "project": "longhouse",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": now.isoformat(),
            "last_activity_at": now.isoformat(),
            "ended_at": None,
            "origin_kind": "shadow",
            "hidden_from_default_timeline": False,
            "launch_actor": None,
            "launch_surface": None,
        },
        "sealed_at": now.isoformat(),
    }
    return params, sealed_raw, sealed_render


def _timeline_params() -> dict[str, object]:
    return {
        "owner_id": "42",
        "before_last_activity_at": None,
        "before_session_id": None,
        "project": None,
        "provider": None,
        "include_test": False,
        "limit": 100,
    }


def _search_params(query: str) -> dict[str, object]:
    return {
        "owner_id": "42",
        "query": query,
        "project": None,
        "provider": None,
        "environment": None,
        "window_start_us": None,
        "window_end_us": None,
        "limit": 10,
    }


@pytest.mark.asyncio
async def test_corrupt_raw_object_is_attributed_without_harming_catalog_or_timeline(tmp_path: Path):
    root = _socket_root("lh-destructive-raw")
    object_root = tmp_path / "objects-v2"
    session_id = uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    params, sealed_raw, _ = _build_storage_commit(object_root, session_id=session_id, text="durable truth", now=now)
    daemon = CatalogDaemon(database_path=root / "live.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    client = CatalogClient(root / "catalogd.sock")
    try:
        await client.call("storage.raw_object.commit.v2", params)
        object_path = object_root / sealed_raw.object_path
        corrupted = bytearray(object_path.read_bytes())
        corrupted[len(corrupted) // 2] ^= 0xFF
        object_path.write_bytes(corrupted)

        with pytest.raises(RawObjectCorruptError, match="hash mismatch"):
            read_raw_object(object_root, sealed_raw.object_path, expected_object_hash=sealed_raw.object_hash)

        manifest = await client.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [sealed_raw.envelope_id]},
        )
        assert manifest["objects"][0]["state"] == "durable"
        assert manifest["objects"][0]["receipt"]["envelope_id"] == sealed_raw.envelope_id
        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        timeline = await client.call("storage.session.timeline.list.v2", _timeline_params())
        assert session["found"] is True and session["deleted"] is False
        assert [row["session_id"] for row in timeline["sessions"]] == [str(session_id)]
    finally:
        await client.close()
        await daemon.close()
        for path in root.iterdir():
            path.unlink(missing_ok=True)
        root.rmdir()


@pytest.mark.asyncio
async def test_corrupt_disposable_search_rebuilds_entirely_from_storage_v2_truth(tmp_path: Path):
    root = _socket_root("lh-destructive-search")
    object_root = tmp_path / "objects-v2"
    search_path = root / "search.db"
    session_id = uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    params, sealed_raw, _ = _build_storage_commit(
        object_root,
        session_id=session_id,
        text="recoverable photon index",
        now=now,
    )
    catalog_daemon = CatalogDaemon(database_path=root / "live.db", socket_path=root / "catalogd.sock")
    search_daemon = SearchDaemon(database_path=search_path, socket_path=root / "searchd.sock")
    await catalog_daemon.start()
    await search_daemon.start()
    catalog = CatalogClient(root / "catalogd.sock")
    search = CatalogClient(root / "searchd.sock")
    reader = _RenderReader(object_root)
    try:
        await catalog.call("storage.raw_object.commit.v2", params)
        first_projector = SearchV2Projector(
            catalog=catalog,
            search=search,
            render_workers=reader,
            worker_id="destructive-proof-first",
        )
        assert await first_projector.run_once(now=now) == 1
        first_store_id = (await search.call("search.ping.v2"))["store_id"]
        assert (await search.call("search.query.v2", _search_params("photon")))["results"][0]["session_id"] == str(
            session_id
        )

        await search.close()
        await search_daemon.close()
        search_path.write_bytes(b"not a sqlite database")

        search_daemon = SearchDaemon(database_path=search_path, socket_path=root / "searchd.sock")
        await search_daemon.start()
        search = CatalogClient(root / "searchd.sock")
        rebuilt_ping = await search.call("search.ping.v2")
        assert rebuilt_ping["store_id"] != first_store_id
        assert rebuilt_ping["published_sessions"] == 0

        second_projector = SearchV2Projector(
            catalog=catalog,
            search=search,
            render_workers=reader,
            worker_id="destructive-proof-rebuild",
        )
        assert await second_projector.run_once(now=now + timedelta(seconds=1)) == 1
        rebuilt = await search.call("search.query.v2", _search_params("photon"))
        assert rebuilt["results"][0]["session_id"] == str(session_id)
        truth = await catalog.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [sealed_raw.envelope_id]},
        )
        assert truth["objects"][0]["state"] == "durable"
    finally:
        await search.close()
        await search_daemon.close()
        await catalog.close()
        await catalog_daemon.close()
        for path in root.iterdir():
            path.unlink(missing_ok=True)
        root.rmdir()


@pytest.mark.asyncio
async def test_session_tombstone_fences_retried_and_late_machine_agent_commits(tmp_path: Path):
    root = _socket_root("lh-destructive-delete")
    object_root = tmp_path / "objects-v2"
    session_id = uuid4()
    now = datetime.now(UTC).replace(microsecond=0)
    original, _, _ = _build_storage_commit(object_root, session_id=session_id, text="before delete", now=now)
    late, _, _ = _build_storage_commit(
        object_root,
        session_id=session_id,
        text="late machine-agent retry",
        now=now + timedelta(seconds=2),
    )
    daemon = CatalogDaemon(database_path=root / "live.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    client = CatalogClient(root / "catalogd.sock")
    try:
        await client.call("storage.raw_object.commit.v2", original)
        deleted = await client.call(
            "storage.session.delete.v2",
            {
                "session_id": str(session_id),
                "deletion_id": str(uuid4()),
                "reason": "user_requested",
                "deleted_at": (now + timedelta(seconds=1)).isoformat(),
            },
        )
        for attempted in (original, late):
            with pytest.raises(CatalogRemoteError) as fenced:
                await client.call("storage.raw_object.commit.v2", attempted)
            assert fenced.value.code == "session_deleted"
            assert fenced.value.details["deletion_revision"] == deleted["deletion_revision"]

        session = await client.call("storage.session.read.v2", {"session_id": str(session_id)})
        timeline = await client.call("storage.session.timeline.list.v2", _timeline_params())
        assert session["found"] is False and session["deleted"] is True
        assert timeline["sessions"] == []
    finally:
        await client.close()
        await daemon.close()
        for path in root.iterdir():
            path.unlink(missing_ok=True)
        root.rmdir()
