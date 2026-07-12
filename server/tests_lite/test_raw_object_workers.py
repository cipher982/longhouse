from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.raw_object_workers import RawObjectWorkerBusy
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.raw_objects import read_raw_object


def _spec() -> RawObjectSpec:
    return RawObjectSpec(
        tenant_id="single",
        machine_id="cinder",
        session_id=uuid4(),
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=uuid4(),
        range_kind="byte_offset",
        range_start=0,
        range_end=6,
        records=(RawRecord(source_position=0, data=b"hello\n"),),
    )


@pytest.mark.asyncio
async def test_process_pool_seals_live_and_repair_objects_without_sharing_capacity(tmp_path):
    pool = RawObjectWorkerPool(tmp_path, live_workers=1, repair_workers=1, queue_multiplier=1)
    try:
        await pool.start()
        spec = _spec()
        live, repair = await asyncio.gather(
            pool.seal(spec, lane="live"),
            pool.seal(spec, lane="repair"),
        )
        assert live.object_hash == repair.object_hash
        replay = await pool.seal(spec, lane="live")
        assert replay.reused is True
        decoded = read_raw_object(tmp_path, live.object_path, expected_object_hash=live.object_hash)
        assert decoded.spec == spec
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_timed_out_seal_holds_capacity_until_child_finishes(tmp_path):
    pool = RawObjectWorkerPool(tmp_path, live_workers=1, repair_workers=1, queue_multiplier=1)
    try:
        await pool.start()
        spec = _spec()
        spec = RawObjectSpec(
            tenant_id=spec.tenant_id,
            machine_id=spec.machine_id,
            session_id=spec.session_id,
            provider=spec.provider,
            opaque_source_id=spec.opaque_source_id,
            source_epoch=spec.source_epoch,
            range_kind=spec.range_kind,
            range_start=0,
            range_end=4 * 1024 * 1024,
            records=(RawRecord(source_position=0, data=bytes(range(256)) * (16 * 1024)),),
        )
        with pytest.raises(RawObjectWorkerError, match="exceeded its deadline"):
            await pool.seal(spec, lane="live", operation_timeout_seconds=1e-9)
        assert pool._live_slots.locked()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_admission_is_bounded_and_repair_has_reserved_capacity(tmp_path):
    pool = RawObjectWorkerPool(tmp_path, live_workers=1, repair_workers=1, queue_multiplier=1)
    try:
        async with pool.admission("live"):
            with pytest.raises(RawObjectWorkerBusy, match="live admission queue is full"):
                async with pool.admission("live", queue_timeout_seconds=1e-9):
                    raise AssertionError("full live admission queue was entered")
            async with pool.admission("repair", queue_timeout_seconds=0.1):
                pass
    finally:
        await pool.close()
