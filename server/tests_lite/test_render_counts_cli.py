import json
import sqlite3
from uuid import uuid4

import pytest
from zerg.cli.render_counts import repair_counts
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.render_objects import seal_render_object


@pytest.fixture
def legacy_render(tmp_path):
    root = tmp_path / "objects"
    spec = RenderObjectSpec(
        session_id=uuid4(),
        render_generation=uuid4(),
        parser_revision="fixture",
        ordering_revision="semantic-order-v2",
        machine_id="fixture",
        provider="cursor",
        opaque_source_id="fixture-source",
        source_epoch=uuid4(),
        source_envelope_id="a" * 64,
        records=(
            RenderRecord(
                event_id="partial",
                order_time_us=1,
                source_position=0,
                event_subordinal=0,
                role="assistant",
                content_text="partial answer",
                branch_kind="abandoned",
                interaction_kind="provider_system",
            ),
        ),
    )
    sealed = seal_render_object(root, spec)
    database = tmp_path / "catalog.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sessions (session_id TEXT, current_render_generation TEXT, owner_id TEXT)")
        connection.execute(
            "CREATE TABLE render_objects (object_id TEXT, session_id TEXT, generation_id TEXT, object_path TEXT, object_hash TEXT, event_count INTEGER, source_envelope_id TEXT, retired_at TEXT)"
        )
        connection.execute("INSERT INTO sessions VALUES (?,?,?)", (str(spec.session_id), str(spec.render_generation), "owner"))
        connection.execute(
            "INSERT INTO render_objects VALUES (?,?,?,?,?,?,?,NULL)",
            (
                sealed.object_id,
                str(spec.session_id),
                str(spec.render_generation),
                sealed.object_path,
                sealed.object_hash,
                1,
                spec.source_envelope_id,
            ),
        )
    receipt = {
        "object_id": sealed.object_id,
        "object_hash": sealed.object_hash,
        "session_id": str(spec.session_id),
        "generation_id": str(spec.render_generation),
        "event_count": 1,
        "owner_id": "owner",
        "abandoned_events": 1,
    }
    return database, root, sealed, receipt


@pytest.mark.asyncio
async def test_precompute_revalidates_cache_against_immutable_object(legacy_render, tmp_path):
    database, root, sealed, receipt = legacy_render
    cache = tmp_path / "counts.jsonl"
    cache.write_text(json.dumps({**receipt, "object_hash": "b" * 64, "abandoned_events": 0}) + "\n")
    native_before = (root / sealed.object_path).read_bytes()
    result = await repair_counts(database=database, cache=cache, socket_path=None, object_root=root, apply=False, limit=None)
    assert result["status"] == "pass"
    assert result["computed"] == 1
    assert json.loads(cache.read_text().splitlines()[-1])["abandoned_events"] == 1
    assert (root / sealed.object_path).read_bytes() == native_before


@pytest.mark.asyncio
async def test_corrupt_render_cannot_leave_a_stale_successful_repair_receipt(legacy_render, tmp_path):
    database, root, sealed, _ = legacy_render
    cache = tmp_path / "counts.jsonl"
    cache.with_suffix(".summary.json").write_text('{"status":"pass"}\n')
    path = root / sealed.object_path
    corrupted = path.read_bytes() + b"corrupt"
    path.write_bytes(corrupted)
    result = await repair_counts(database=database, cache=cache, socket_path=None, object_root=root, apply=False, limit=None)
    assert result["status"] == "fail"
    assert result["errors"][0]["object_id"] == sealed.object_id
    assert json.loads(cache.with_suffix(".summary.json").read_text())["status"] == "fail"
    assert cache.read_text() == ""
    assert path.read_bytes() == corrupted
