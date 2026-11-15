"""Derived work never competes with source and control writes for the catalog writer."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tests_lite.test_catalogd_heartbeat import _heartbeat
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.server import CatalogWriterBusy
from zerg.catalogd.server import CatalogWriterStats
from zerg.catalogd.writer_lanes import DERIVED
from zerg.catalogd.writer_lanes import FAIR_SHARE
from zerg.catalogd.writer_lanes import PRIMARY
from zerg.catalogd.writer_lanes import WriterLanes
from zerg.services import render_shadow


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-lanes-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def test_primary_work_runs_before_queued_derived_work_with_a_fair_share():
    lanes = WriterLanes(thread_name="test-lanes")
    gate = threading.Event()
    order: list[str] = []
    try:
        blocker = lanes.submit(PRIMARY, lambda: gate.wait(5))
        derived = [lanes.submit(DERIVED, lambda i=i: order.append(f"d{i}")) for i in range(3)]
        primary = [lanes.submit(PRIMARY, lambda i=i: order.append(f"p{i}")) for i in range(FAIR_SHARE + 2)]
        gate.set()
        for future in [blocker, *derived, *primary]:
            future.result(timeout=5)
    finally:
        lanes.shutdown()
    # Every primary write queued behind a running one goes first, up to the
    # fair share; derived work then gets a turn instead of starving.
    # (the blocker was the first primary item of the streak)
    assert order[: FAIR_SHARE - 1] == [f"p{i}" for i in range(FAIR_SHARE - 1)]
    assert order[FAIR_SHARE - 1] == "d0"
    assert sorted(order) == sorted([f"d{i}" for i in range(3)] + [f"p{i}" for i in range(FAIR_SHARE + 2)])


def test_derived_flood_cannot_take_primary_capacity():
    stats = CatalogWriterStats()
    accepted = [stats.admit(128, lane=DERIVED, derived_max_depth=8) for _ in range(50)]
    assert accepted.count("accepted") == 8
    assert accepted.count("busy") == 42
    # All 128 primary slots are still available to heartbeats and ingest.
    assert [stats.admit(128, lane=PRIMARY, derived_max_depth=8) for _ in range(128)] == ["accepted"] * 128
    assert stats.admit(128, lane=PRIMARY, derived_max_depth=8) == "busy"


def test_derived_work_backs_off_while_the_primary_queue_is_half_full():
    stats = CatalogWriterStats()
    for _ in range(64):
        assert stats.admit(128, lane=PRIMARY) == "accepted"
    assert stats.admit(128, lane=DERIVED) == "busy"
    stats.record_dequeue(PRIMARY)
    assert stats.admit(128, lane=DERIVED) == "accepted"


@pytest.mark.asyncio
async def test_heartbeats_are_accepted_while_derived_work_floods_the_writer(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    await daemon._projector_repair_task
    client = CatalogClient(socket_path)
    entered = threading.Event()
    release = threading.Event()
    ran: list[str] = []

    def hold_writer():
        entered.set()
        assert release.wait(timeout=5)

    async def derived_work(i: int):
        return await daemon._run_store(lambda: ran.append(f"derived{i}"), lane=DERIVED)

    holder = asyncio.create_task(daemon._run_store(hold_writer, lane=DERIVED))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        flood = [asyncio.create_task(derived_work(i)) for i in range(200)]
        await asyncio.sleep(0.1)
        busy = [task for task in flood if task.done() and isinstance(task.exception(), CatalogWriterBusy)]
        # Derived admission is capped: the flood is turned away, not queued.
        assert len(busy) >= 150
        # Derived work saturating its lane through the RPC surface is refused
        # with a retry hint, while a heartbeat is still admitted and runs first.
        with pytest.raises(CatalogRemoteError) as refused:
            await client.call("render.shadow.record.v2", {"results": []})
        assert refused.value.code == "resource_exhausted"
        heartbeat = asyncio.create_task(
            client.call(
                "machine.heartbeat.apply.v2",
                {
                    "heartbeat": _heartbeat(device_id="cinder", received_at=datetime.now(UTC), digest="d"),
                    "managed_leases": [],
                    "managed_leases_present": False,
                    "owner_id": 7,
                },
            )
        )
        await asyncio.sleep(0.1)
        release.set()
        applied = await asyncio.wait_for(heartbeat, 5)
        assert applied["exact_replay"] is False
        accepted = [task for task in flood if task not in busy]
        await asyncio.gather(*accepted)
        # The heartbeat ran before the derived work that was already queued.
        assert len(ran) == len(accepted)
        assert daemon._writer_stats.snapshot()["derived_rejected"] >= len(busy)
    finally:
        release.set()
        await holder
        await client.close()
        await daemon.close()


class _FakeCatalog:
    def __init__(self, depths):
        self.depths = list(depths)

    async def call(self, method, params=None):
        assert method == "ping.v2"
        depth, derived = self.depths.pop(0) if len(self.depths) > 1 else self.depths[0]
        return {"writer_admission": {"depth": depth, "derived_depth": derived}}


@pytest.mark.asyncio
async def test_derived_sweep_waits_for_primary_headroom_and_gives_up_when_busy(monkeypatch):
    monkeypatch.setattr(render_shadow, "HEADROOM_WAIT_SECONDS", 0.01)
    # Derived queue depth does not count as busy; a deep primary queue does.
    assert await render_shadow.wait_for_writer_headroom(_FakeCatalog([(30, 30)]), max_wait_seconds=1) is True
    assert await render_shadow.wait_for_writer_headroom(_FakeCatalog([(60, 2), (50, 2), (3, 0)]), max_wait_seconds=5) is True
    assert await render_shadow.wait_for_writer_headroom(_FakeCatalog([(60, 2)]), max_wait_seconds=0.1) is False
