from __future__ import annotations

import asyncio
import os
import signal
import time
from uuid import uuid4

import pytest

import zerg.services.raw_object_workers as worker_module
from zerg.services.raw_object_workers import RawObjectWorkerBusy
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.render_object_workers import RenderObjectWorkerBusy
from zerg.services.render_object_workers import RenderObjectWorkerError
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.storage_v2.raw_objects import RawObjectSpec
from zerg.storage_v2.raw_objects import RawRecord
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord


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


def _render_spec() -> RenderObjectSpec:
    return RenderObjectSpec(
        session_id=uuid4(),
        render_generation=uuid4(),
        parser_revision="engine-parser-v2",
        ordering_revision="semantic-order-v2",
        machine_id="cinder",
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=uuid4(),
        source_envelope_id="a" * 64,
        records=(
            RenderRecord(
                event_id="user-1",
                order_time_us=1_700_000_000_000_000,
                source_position=0,
                event_subordinal=0,
                role="user",
                content_text="hello",
            ),
        ),
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
@pytest.mark.parametrize(
    ("pool_type", "worker_error", "worker_busy"),
    [
        (RawObjectWorkerPool, RawObjectWorkerError, RawObjectWorkerBusy),
        (RenderObjectWorkerPool, RenderObjectWorkerError, RenderObjectWorkerBusy),
    ],
)
async def test_failed_operation_cleanup_is_bounded_and_next_operation_recovers(
    tmp_path,
    monkeypatch,
    pool_type,
    worker_error,
    worker_busy,
):
    pool = pool_type(tmp_path, live_workers=1, repair_workers=1, user_read_workers=1, queue_multiplier=1)
    original = worker_module._terminate_owned_executor
    cleanup_started = asyncio.Event()
    loop = asyncio.get_running_loop()
    stopped = None
    try:
        await pool.start()
        spec = _spec() if pool_type is RawObjectWorkerPool else _render_spec()
        sealed = await pool.seal(spec, lane="live")
        stopped = next(iter(pool._repair_pool.executor._processes.values()))
        os.kill(stopped.pid, signal.SIGSTOP)

        def fail_cleanup(executor, _processes):
            loop.call_soon_threadsafe(cleanup_started.set)
            return False

        monkeypatch.setattr(worker_module, "_terminate_owned_executor", fail_cleanup)
        with pytest.raises(worker_error, match="deadline"):
            await pool.seal(spec, lane="repair", operation_timeout_seconds=0.05)
        await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
        await asyncio.sleep(0)

        with pytest.raises(worker_busy, match="queue|unavailable"):
            await pool.seal(spec, lane="repair", operation_timeout_seconds=0.1, queue_timeout_seconds=0.1)
        live_replay = await pool.seal(spec, lane="live")
        assert live_replay.object_hash == sealed.object_hash
        assert stopped.is_alive()

        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)
        if pool_type is RawObjectWorkerPool:
            recovered = await pool.read(
                sealed.object_path,
                sealed.object_hash,
                spec.tenant_id,
                lane="repair",
            )
        else:
            recovered = await pool.read(sealed.object_path, sealed.object_hash, lane="background")
        assert recovered.spec == spec
        assert not stopped.is_alive()
    finally:
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)
        try:
            await asyncio.wait_for(pool.close(), timeout=5.0)
        finally:
            if stopped is not None and stopped.is_alive():
                stopped.kill()
                stopped.join(3.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_type", [RawObjectWorkerPool, RenderObjectWorkerPool])
async def test_broken_pool_cleanup_terminates_surviving_owned_child(tmp_path, monkeypatch, pool_type):
    pool = pool_type(tmp_path, live_workers=1, repair_workers=2, user_read_workers=1, queue_multiplier=1)
    original = worker_module._terminate_owned_executor
    children = []
    try:
        await pool.start()
        spec = _spec() if pool_type is RawObjectWorkerPool else _render_spec()
        sealed = await pool.seal(spec, lane="live")

        async def repair_read():
            if pool_type is RawObjectWorkerPool:
                return await pool.read(sealed.object_path, sealed.object_hash, spec.tenant_id, lane="repair")
            return await pool.read(sealed.object_path, sealed.object_hash, lane="background")

        executor = pool._repair_pool.executor
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            loop.run_in_executor(executor, time.sleep, 0.1),
            loop.run_in_executor(executor, time.sleep, 0.1),
        )
        children = list(pool._repair_pool.executor._processes.values())
        assert len(children) >= 2
        broken, survivor = children[:2]
        assert survivor.is_alive()
        os.kill(survivor.pid, signal.SIGSTOP)
        os.kill(broken.pid, signal.SIGKILL)
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", lambda *_: False)
        worker_busy = RawObjectWorkerBusy if pool_type is RawObjectWorkerPool else RenderObjectWorkerBusy
        with pytest.raises(worker_busy, match="unavailable"):
            await repair_read()
        assert survivor.is_alive()
        live_replay = await pool.seal(spec, lane="live")
        assert live_replay.object_hash == sealed.object_hash
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)

        recovered = await repair_read()
        assert recovered.spec == spec
        assert not survivor.is_alive()
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
