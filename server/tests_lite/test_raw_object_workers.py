from __future__ import annotations

import asyncio
import os
import signal
from uuid import uuid4

import pytest

import zerg.services.raw_object_workers as worker_module
from zerg.services.raw_object_workers import RawObjectWorkerBusy
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord


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
        decoded = await pool.read(live.object_path, live.object_hash, spec.tenant_id)
        assert decoded.spec == spec
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_identical_user_reads_share_one_inflight_decode(tmp_path, monkeypatch):
    pool = RawObjectWorkerPool(tmp_path, live_workers=1, repair_workers=1, user_read_workers=1, queue_multiplier=1)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    decoded = object()

    async def slow_read(object_path, expected_object_hash, tenant_id, **_kwargs):
        nonlocal calls
        calls += 1
        assert object_path == "raw.zst"
        assert expected_object_hash == "a" * 64
        assert tenant_id == "single"
        started.set()
        await release.wait()
        return decoded

    monkeypatch.setattr(pool, "_read_with_recovery", slow_read)
    reads = [asyncio.create_task(pool.read("raw.zst", "a" * 64, "single")) for _ in range(8)]
    try:
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(*reads) == [decoded] * 8
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(*reads, return_exceptions=True)
        await pool.close()


@pytest.mark.asyncio
async def test_stopped_repair_read_leaves_user_and_live_work_available(tmp_path):
    pool = RawObjectWorkerPool(tmp_path, live_workers=1, repair_workers=1, user_read_workers=1, queue_multiplier=1)
    stopped = None
    pending = None
    try:
        await pool.start()
        spec = _spec()
        sealed = await pool.seal(spec, lane="live")
        stopped = next(iter(pool._repair_pool.executor._processes.values()))
        os.kill(stopped.pid, signal.SIGSTOP)
        pending = asyncio.create_task(
            pool.read(sealed.object_path, sealed.object_hash, spec.tenant_id, lane="repair", operation_timeout_seconds=1.0)
        )
        await asyncio.sleep(0)
        decoded = await pool.read(sealed.object_path, sealed.object_hash, spec.tenant_id, lane="user", operation_timeout_seconds=1.0)
        assert decoded.spec == spec
        fresh_spec = _spec()
        fresh = await pool.seal(fresh_spec, lane="live")
        assert (await pool.read(fresh.object_path, fresh.object_hash, fresh_spec.tenant_id)).spec == fresh_spec
        with pytest.raises(RawObjectWorkerError, match="deadline"):
            await pending
        await asyncio.to_thread(stopped.join, 3.0)
        assert not stopped.is_alive()
        assert (await pool.read(sealed.object_path, sealed.object_hash, spec.tenant_id, lane="repair")).spec == spec
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await asyncio.wait_for(pool.close(), timeout=5.0)
        finally:
            if stopped is not None and stopped.is_alive():
                stopped.kill()
                stopped.join(3.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_type", [RawObjectWorkerPool, RenderObjectWorkerPool])
async def test_failed_close_reports_failure_and_can_retry_child_cleanup(tmp_path, monkeypatch, pool_type):
    pool = pool_type(tmp_path, live_workers=1, repair_workers=1, user_read_workers=1)
    original = worker_module._terminate_owned_executor
    children = []
    try:
        await pool.start()
        children = [
            child for owner in (pool._live_pool, pool._repair_pool, pool._user_read_pool) for child in owner.executor._processes.values()
        ]
        stopped = next(iter(pool._repair_pool.executor._processes.values()))
        os.kill(stopped.pid, signal.SIGSTOP)

        def fail_cleanup(executor, _processes):
            executor.shutdown(wait=False, cancel_futures=True)
            return False

        monkeypatch.setattr(worker_module, "_terminate_owned_executor", fail_cleanup)
        with pytest.raises(RuntimeError, match="could not be stopped"):
            await pool.close()
        assert stopped.is_alive()
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)
        await asyncio.wait_for(pool.close(), timeout=5.0)
        assert all(not child.is_alive() for child in children)
    finally:
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)
        try:
            await asyncio.wait_for(pool.close(), timeout=5.0)
        finally:
            for child in children:
                if child.is_alive():
                    child.kill()
                    child.join(3.0)


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
