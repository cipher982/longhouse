"""The legacy-twin evidence rule: a legacy copy qualifies only when its native twin holds it all."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import zstandard

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.server import CatalogDaemon
from zerg.services.legacy_twins import find_legacy_twins
from zerg.storage_v2.normalized_events import opaque_source_id
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import seal_raw_object
from zerg.storage_v2.render_objects import SEMANTIC_PROJECTION_VERSION
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import seal_render_object

START = datetime(2026, 7, 9, 3, 47, 8, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path):
    socket_dir = Path("/tmp") / f"lhcd-twin-{uuid4().hex[:10]}"
    socket_dir.mkdir(mode=0o700)
    yield tmp_path / "live.db", socket_dir / "catalogd.sock", tmp_path / "objects", tmp_path / "archive"
    for path in socket_dir.iterdir():
        path.unlink(missing_ok=True)
    socket_dir.rmdir()


async def _commit(client, objects: Path, *, session_id, provenance: str, texts: list[tuple[str, str]], records: int, started_at):
    source = opaque_source_id(session_id, f"src:{session_id}", provenance)
    epoch = uuid4()
    raw = RawObjectSpec(
        tenant_id="tenant-a",
        machine_id="cinder",
        session_id=session_id,
        provider="cursor",
        opaque_source_id=source,
        source_epoch=epoch,
        range_kind="record_ordinal",
        range_start=0,
        range_end=records,
        records=tuple(RawRecord(source_position=index, data=f"record-{session_id}-{index}".encode()) for index in range(records)),
        provenance_kind=provenance,
    )
    sealed_raw = seal_raw_object(objects, raw)
    render = RenderObjectSpec(
        session_id=session_id,
        render_generation=uuid4(),
        parser_revision="test",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="cursor",
        opaque_source_id=source,
        source_epoch=epoch,
        source_envelope_id=sealed_raw.envelope_id,
        records=tuple(
            RenderRecord(
                event_id=f"e{index}",
                order_time_us=int(started_at.timestamp() * 1_000_000) + index,
                source_position=index,
                event_subordinal=index,
                role=role,
                content_text=text,
                raw_record_ordinal=index,
            )
            for index, (role, text) in enumerate(texts)
        ),
    )
    sealed = seal_render_object(objects, render)
    await client.call(
        "storage.raw_object.commit.v2",
        {
            "protocol_version": 2,
            "tenant_id": "tenant-a",
            "owner_id": "42",
            "session_id": str(session_id),
            "machine_id": "cinder",
            "provider": "cursor",
            "opaque_source_id": source,
            "source_epoch": str(epoch),
            "predecessor_source_epoch": None,
            "epoch_opened_at": started_at.isoformat(),
            "range_kind": "record_ordinal",
            "range_start": 0,
            "range_end": records,
            "record_hashes": list(sealed_raw.record_hashes),
            "envelope_id": sealed_raw.envelope_id,
            "object_hash": sealed_raw.object_hash,
            "payload_hash": sealed_raw.payload_hash,
            "compressed_hash": sealed_raw.compressed_hash,
            "object_path": sealed_raw.object_path,
            "uncompressed_size": sealed_raw.uncompressed_size,
            "compressed_size": sealed_raw.compressed_size,
            "provenance_kind": provenance,
            "render_state": "ready",
            "media_refs": [],
            "projectors": [],
            "render_manifest": {
                "generation_id": str(render.render_generation),
                "parser_revision": render.parser_revision,
                "ordering_revision": render.ordering_revision,
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
                "abandoned_events": sealed.abandoned_events,
                "first_user_message_preview": sealed.first_user_message_preview,
                "last_visible_text_preview": sealed.last_visible_text_preview,
                "semantic_projection_version": SEMANTIC_PROJECTION_VERSION,
            },
            "session_facts": {
                "environment": "production",
                "project": "zerg",
                "cwd": "/repo",
                "git_repo": None,
                "git_branch": None,
                "started_at": started_at.isoformat(),
                "last_activity_at": started_at.isoformat(),
                "ended_at": None,
                "origin_kind": None,
                "hidden_from_default_timeline": False,
                "launch_actor": None,
                "launch_surface": None,
            },
            "conversation_resets": [],
            "sealed_at": started_at.isoformat(),
        },
    )


def _archive(archive: Path, session_id, lines: int) -> None:
    chunks = archive / "tenants" / "tenant-a" / "sessions" / str(session_id) / "chunks"
    chunks.mkdir(parents=True)
    payload = b"\n".join(json.dumps({"raw_sha256": f"{index:064x}", "raw_b64": ""}).encode() for index in range(lines))
    (chunks / "source_lines-0.jsonl.zst").write_bytes(zstandard.ZstdCompressor().compress(payload))


@pytest.mark.asyncio
async def test_a_legacy_copy_qualifies_only_against_a_twin_that_holds_all_of_it(paths, monkeypatch):
    live, socket, objects, archive = paths
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    daemon = CatalogDaemon(database_path=live, socket_path=socket)
    await daemon.start()
    client = CatalogClient(socket)
    conversation = [
        ("user", "<timestamp>Wed</timestamp> <user_query> fix the build </user_query>"),
        ("assistant", "Looking at the failing step."),
        ("assistant", "Fixed:  the  image now builds."),
    ]
    native_conversation = [
        ("user", "fix the build"),
        ("assistant", "Looking at the failing step."),
        ("assistant", "Fixed: the image now builds."),
    ]
    legacy, twin, partial_legacy, partial_twin, unarchived, unarchived_twin = (uuid4() for _ in range(6))
    try:
        await _commit(
            client, objects, session_id=legacy, provenance="legacy_normalized_event", texts=conversation, records=3, started_at=START
        )
        await _commit(
            client,
            objects,
            session_id=twin,
            provenance="native",
            texts=native_conversation,
            records=8,
            started_at=START + timedelta(seconds=1),
        )
        _archive(archive, legacy, lines=5)
        later = START + timedelta(hours=1)
        await _commit(
            client,
            objects,
            session_id=partial_legacy,
            provenance="legacy_normalized_event",
            texts=conversation,
            records=3,
            started_at=later,
        )
        await _commit(
            client, objects, session_id=partial_twin, provenance="native", texts=native_conversation[:2], records=2, started_at=later
        )
        _archive(archive, partial_legacy, lines=5)
        far = START + timedelta(hours=2)
        await _commit(
            client, objects, session_id=unarchived, provenance="legacy_normalized_event", texts=conversation, records=3, started_at=far
        )
        await _commit(
            client, objects, session_id=unarchived_twin, provenance="native", texts=native_conversation, records=8, started_at=far
        )
    finally:
        await client.close()
        await daemon.close()

    evidence = {item.legacy_session_id: item for item in find_legacy_twins(live_database=live, object_root=objects, archive_root=archive)}
    assert set(evidence) == {str(legacy), str(partial_legacy), str(unarchived)}

    full = evidence[str(legacy)]
    assert full.twin_session_id == str(twin)
    assert full.qualifies, full.reasons

    partial = evidence[str(partial_legacy)]
    assert not partial.qualifies
    assert set(partial.reasons) == {"assistant_text_missing_from_twin", "native_records_below_archive_lines"}

    # Without archive evidence the record-count check cannot be made, so it never passes silently.
    assert evidence[str(unarchived)].reasons == ["archive_lines_unavailable"]

    # Nothing outside the window counts as a twin.
    assert find_legacy_twins(live_database=live, object_root=objects, archive_root=archive, session_ids=[str(legacy)], window_seconds=1)[
        0
    ].qualifies
