from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from uuid import uuid4

import pytest

import zerg.services.raw_object_workers as worker_module
from tests_lite._process_helpers import child_is_gone
from tests_lite._process_helpers import ignore_sigterm_and_park
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
        assert child_is_gone(stopped)
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
        assert child_is_gone(stopped)
    finally:
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)
        try:
            await asyncio.wait_for(pool.close(), timeout=5.0)
        finally:
            if stopped is not None and stopped.is_alive():
                stopped.kill()
                stopped.join(3.0)


def _wait_until_stopped(pid: int) -> None:
    """Block until the kernel reports ``pid`` stopped, not merely sent SIGSTOP.

    While both are pending, Linux dequeues SIGTERM (15) before SIGSTOP (19), so
    the stdlib's broken-pool ``terminate()`` could kill a child that had not yet
    stopped, and this test raced it.
    """
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            if handle.read().rsplit(")", 1)[1].split()[0] in {"T", "t"}:
                return
        time.sleep(0.01)
    raise AssertionError(f"process {pid} never stopped")


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Darwin delivers SIGTERM to a stopped process, so the stdlib's broken-pool terminate() cannot be held off",
)
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
        _wait_until_stopped(survivor.pid)
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", lambda *_: False)
        worker_busy = RawObjectWorkerBusy if pool_type is RawObjectWorkerPool else RenderObjectWorkerBusy
        # Queue the read while the pool is still whole so the outcome is fixed:
        # a submit that arrives after the manager thread noticed the death
        # blocks on the executor's shutdown lock instead, and times out (that
        # ordering is test_submit_to_a_broken_pool_never_blocks_the_event_loop).
        # So stop both children, queue the read behind them, and only then
        # kill one.
        os.kill(broken.pid, signal.SIGSTOP)
        _wait_until_stopped(broken.pid)
        read = asyncio.create_task(repair_read())
        async with asyncio.timeout(10):
            while not executor._pending_work_items:
                await asyncio.sleep(0.005)
        os.kill(broken.pid, signal.SIGKILL)
        with pytest.raises(worker_busy, match="unavailable"):
            await read
        assert survivor.is_alive()
        live_replay = await pool.seal(spec, lane="live")
        assert live_replay.object_hash == sealed.object_hash
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)

        recovered = await repair_read()
        assert recovered.spec == spec
        assert child_is_gone(survivor)
    finally:
        monkeypatch.setattr(worker_module, "_terminate_owned_executor", original)
        try:
            await asyncio.wait_for(pool.close(), timeout=5.0)
        finally:
            for child in children:
                if child.is_alive():
                    child.kill()
                    child.join(3.0)


async def _break_repair_pool_around_a_sigterm_proof_child(pool, ready):
    """Kill one repair child while the other ignores SIGTERM.

    Returns once the stdlib manager thread has marked the pool broken and is
    joining the survivor, so it holds the executor's shutdown lock until the
    survivor is killed.
    """
    executor = pool._repair_pool.executor
    loop = asyncio.get_running_loop()
    parked = [loop.run_in_executor(executor, ignore_sigterm_and_park, str(ready), 60.0) for _ in range(2)]
    async with asyncio.timeout(15):
        while len(list(ready.iterdir())) < 2:
            await asyncio.sleep(0.01)
    children = list(executor._processes.values())
    assert len(children) >= 2
    broken, survivor = children[:2]
    os.kill(broken.pid, signal.SIGKILL)
    async with asyncio.timeout(10):
        while not executor._broken:
            await asyncio.sleep(0.005)
    await asyncio.gather(*parked, return_exceptions=True)
    assert executor._shutdown_lock.locked()
    assert survivor.is_alive()
    return executor, broken, survivor, children


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pool_type", "worker_error"),
    [(RawObjectWorkerPool, RawObjectWorkerError), (RenderObjectWorkerPool, RenderObjectWorkerError)],
)
async def test_submit_to_a_broken_pool_never_blocks_the_event_loop(tmp_path, pool_type, worker_error):
    """One dead child plus one SIGTERM-proof child must not wedge the API loop.

    The stdlib manager thread holds the executor's shutdown lock while it joins
    the survivor, and ``submit`` takes the same lock. Submitting from the loop
    thread therefore froze every route on the host until something killed the
    survivor, which only the frozen loop could do. A heartbeat measured from a
    plain thread, so a stopped loop is observed rather than hanging the test,
    proves the loop keeps ticking; the operation then fails with the pool's
    own typed error at its deadline and the next one recovers.
    """
    pool = pool_type(tmp_path, live_workers=1, repair_workers=2, user_read_workers=1, queue_multiplier=1)
    ready = tmp_path / "ready"
    ready.mkdir()
    last_tick = time.monotonic()
    stop_watching = threading.Event()
    loop_stalled = threading.Event()
    children = []
    heartbeat = None
    watcher = None
    try:
        await pool.start()
        spec = _spec() if pool_type is RawObjectWorkerPool else _render_spec()
        sealed = await pool.seal(spec, lane="live")

        async def repair_read():
            if pool_type is RawObjectWorkerPool:
                return await pool.read(
                    sealed.object_path,
                    sealed.object_hash,
                    spec.tenant_id,
                    lane="repair",
                    operation_timeout_seconds=1.0,
                )
            return await pool.read(sealed.object_path, sealed.object_hash, lane="background", operation_timeout_seconds=1.0)

        executor, broken, survivor, children = await _break_repair_pool_around_a_sigterm_proof_child(pool, ready)

        async def beat():
            nonlocal last_tick
            while True:
                last_tick = time.monotonic()
                await asyncio.sleep(0.01)

        def watch():
            while not stop_watching.wait(0.05):
                if time.monotonic() - last_tick > 1.5:
                    loop_stalled.set()
                    # Free the lock so a regression fails this test instead of hanging the run.
                    os.kill(survivor.pid, signal.SIGKILL)
                    return

        heartbeat = asyncio.create_task(beat())
        last_tick = time.monotonic()
        watcher = threading.Thread(target=watch, name="loop-stall-watcher", daemon=True)
        watcher.start()

        started = time.monotonic()
        outcome = (await asyncio.gather(repair_read(), return_exceptions=True))[0]
        elapsed = time.monotonic() - started
        assert not loop_stalled.is_set(), "the event loop stopped responding while a broken pool was submitted to"
        assert isinstance(outcome, worker_error), outcome
        assert "deadline" in str(outcome)
        assert elapsed < 5.0

        # The abandoned operation's cleanup killed the survivor and the pool
        # recovers: the same read now succeeds on a fresh generation.
        recovered = await repair_read()
        assert recovered.spec == spec
        assert all(child_is_gone(child) for child in children[:2])
    finally:
        stop_watching.set()
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if watcher is not None:
            watcher.join(2.0)
        try:
            await asyncio.wait_for(pool.close(), timeout=5.0)
        finally:
            for child in children:
                if child.is_alive():
                    child.kill()
                    child.join(3.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_type", [RawObjectWorkerPool, RenderObjectWorkerPool])
async def test_close_kills_a_sigterm_proof_child_of_a_broken_pool(tmp_path, pool_type):
    pool = pool_type(tmp_path, live_workers=1, repair_workers=2, user_read_workers=1, queue_multiplier=1)
    ready = tmp_path / "ready"
    ready.mkdir()
    children = []
    try:
        await pool.start()
        _, _, survivor, children = await _break_repair_pool_around_a_sigterm_proof_child(pool, ready)
        await asyncio.wait_for(pool.close(), timeout=5.0)
        assert all(child_is_gone(child) for child in children[:2])
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join(3.0)


@pytest.mark.asyncio
async def test_submit_to_a_retired_generation_is_a_broken_pool_so_the_caller_retries(tmp_path):
    """Between a caller choosing an executor and the dispatcher thread reaching
    it, another operation's cleanup may retire that generation."""
    owner = worker_module._OwnedProcessPool(1)
    retired = owner.executor
    try:
        assert await owner.retire(retired)
        assert owner.executor is not retired
        with pytest.raises(BrokenProcessPool):
            await owner.submit(retired, worker_module._worker_ping)
        assert isinstance(await owner.submit(owner.executor, worker_module._worker_ping), int)
    finally:
        await owner.close()


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
        assert all(child_is_gone(child) for child in children)
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
async def test_repair_write_queues_for_a_slot_while_live_fails_fast(tmp_path):
    """A history import's write waits for an occupied slot instead of bouncing:
    a rejection costs the Machine Agent a 5 s pause of all archive work."""
    pool = RawObjectWorkerPool(tmp_path, live_workers=1, repair_workers=1, queue_multiplier=1)
    try:
        assert worker_module.write_queue_deadline("repair") == worker_module.REPAIR_WRITE_QUEUE_DEADLINE_SECONDS
        assert worker_module.write_queue_deadline("live") == worker_module.LIVE_WRITE_QUEUE_DEADLINE_SECONDS
        assert worker_module.write_queue_deadline("repair", 0.01) == 0.01

        release = asyncio.Event()

        async def hold(lane: str) -> None:
            async with pool.admission(lane):
                await release.wait()

        holders = [asyncio.create_task(hold("repair")), asyncio.create_task(hold("live"))]
        await asyncio.sleep(0)
        asyncio.get_running_loop().call_later(0.5, release.set)
        started = time.monotonic()
        with pytest.raises(RawObjectWorkerBusy, match="live admission queue is full"):
            async with pool.admission("live"):
                raise AssertionError("occupied live admission slot was entered")
        assert time.monotonic() - started < 0.5
        async with pool.admission("repair"):
            assert time.monotonic() - started >= 0.4
        await asyncio.gather(*holders)
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
