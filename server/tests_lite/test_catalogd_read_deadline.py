"""Catalog reads stop once every caller has given up (catalog-read-path.md)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.read_deadline import ReadDeadline
from zerg.catalogd.server import _REQUEST_DEADLINE_NS
from zerg.catalogd.server import CATALOG_INTERACTIVE_READ_WORKERS
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.server import CatalogReadExpired

# Each statement takes a few milliseconds of SQLite VM work, so the progress
# handler runs many times inside every one of them.
_BUSY_STATEMENT = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n LIMIT 20000) SELECT count(*) FROM n"


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhrd-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _busy_read(engine, seconds: float, statements: list[int] | None = None):
    """A read that keeps SQLite busy for `seconds`, one statement after another."""

    def operation(**_key):
        stop = time.monotonic() + seconds
        with engine.connect() as connection:
            while time.monotonic() < stop:
                connection.exec_driver_sql(_BUSY_STATEMENT).scalar_one()
                if statements is not None:
                    statements.append(1)
        return {"done": True}

    return operation


def _in(seconds: float) -> int:
    return time.monotonic_ns() + int(seconds * 1e9)


@pytest.mark.asyncio
async def test_an_abandoned_read_stops_at_its_deadline_and_frees_its_slot(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    try:
        started = time.monotonic()
        with pytest.raises(CatalogReadExpired):
            await daemon._run_read_store(_busy_read(daemon._engine, 5.0), _read_deadline=ReadDeadline(_in(0.05)))
        elapsed = time.monotonic() - started
        assert elapsed < 0.15, elapsed
        assert daemon._read_depth == 0
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_a_read_without_a_deadline_is_never_cut(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    try:
        assert await daemon._run_read_store(_busy_read(daemon._engine, 0.2), _read_deadline=ReadDeadline(None)) == {"done": True}
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_a_shared_read_lives_until_its_last_caller_gives_up(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    statements: list[int] = []
    operation = _busy_read(daemon._engine, 0.3, statements)

    async def caller(deadline_ns: int):
        token = _REQUEST_DEADLINE_NS.set(deadline_ns)
        try:
            return await daemon._coalesced_session_detail_read("test.shared", operation, key="same")
        finally:
            _REQUEST_DEADLINE_NS.reset(token)

    try:
        impatient = asyncio.create_task(caller(_in(0.1)))
        waited = time.monotonic()
        while not statements:
            assert time.monotonic() - waited < 0.08, "the shared read never started"
            await asyncio.sleep(0.002)
        patient = asyncio.create_task(caller(_in(5.0)))
        # The first caller's deadline passes while the read runs; the second
        # caller still wants it, so it finishes for both.
        assert await patient == {"done": True}
        assert await impatient == {"done": True}
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_a_shared_read_stops_when_every_caller_has_given_up(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    operation = _busy_read(daemon._engine, 5.0)

    async def caller(deadline_ns: int):
        token = _REQUEST_DEADLINE_NS.set(deadline_ns)
        try:
            return await daemon._coalesced_session_detail_read("test.shared", operation, key="same")
        finally:
            _REQUEST_DEADLINE_NS.reset(token)

    try:
        started = time.monotonic()
        results = await asyncio.gather(caller(_in(0.05)), caller(_in(0.1)), return_exceptions=True)
        assert all(isinstance(result, CatalogReadExpired) for result in results), results
        assert time.monotonic() - started < 0.25
        assert daemon._read_depth == 0
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_a_read_that_waited_past_its_deadline_never_runs(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    release = threading.Event()
    entered = threading.Semaphore(0)
    ran = []

    def hold():
        entered.release()
        release.wait(5)

    holders = [asyncio.create_task(daemon._run_read_store(hold)) for _ in range(CATALOG_INTERACTIVE_READ_WORKERS)]
    try:
        for _ in range(CATALOG_INTERACTIVE_READ_WORKERS):
            assert await asyncio.to_thread(entered.acquire, True, 1)
        queued = asyncio.create_task(daemon._run_read_store(lambda: ran.append(1), _read_deadline=ReadDeadline(_in(0.02))))
        await asyncio.sleep(0.1)
        release.set()
        with pytest.raises(CatalogReadExpired):
            await queued
        assert ran == []
    finally:
        release.set()
        await asyncio.gather(*holders)
        await daemon.close()


@pytest.mark.asyncio
async def test_writes_are_never_interrupted_while_a_read_is_cut(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    try:
        expired_read = asyncio.create_task(daemon._run_read_store(_busy_read(daemon._engine, 5.0), _read_deadline=ReadDeadline(_in(0.02))))
        write = asyncio.create_task(daemon._run_store(_busy_read(daemon._engine, 0.3)))
        with pytest.raises(CatalogReadExpired):
            await expired_read
        assert await write == {"done": True}
    finally:
        await daemon.close()


@pytest.mark.asyncio
async def test_a_read_its_socket_caller_abandoned_stops_on_the_daemon(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    assert daemon._store is not None
    real_list = daemon._store.list_session_subagents
    engine = daemon._engine

    def slow_list(**kwargs):
        _busy_read(engine, 5.0)()
        return real_list(**kwargs)

    daemon._store.list_session_subagents = slow_list
    client = CatalogClient(socket_path)
    try:
        started = time.monotonic()
        with pytest.raises(CatalogUnavailable):
            await client.call(
                "session.subagents.list.v2",
                {"session_id": str(uuid4()), "owner_id": "7"},
                timeout_seconds=0.2,
            )
        # The read was cut at the caller's deadline, not left running for 5 s.
        while daemon._read_depth:
            assert time.monotonic() - started < 1.0
            await asyncio.sleep(0.01)
    finally:
        daemon._store.list_session_subagents = real_list
        await client.close()
        await daemon.close()


def test_a_fired_deadline_stays_fired_and_refuses_extension():
    deadline = ReadDeadline(_in(-0.001))
    assert deadline.trip_if_expired() is True
    # A caller arriving while the cut read unwinds cannot revive it.
    assert deadline.extend(_in(5.0)) is False
    assert deadline.trip_if_expired() is True
    fresh = ReadDeadline(_in(0.01))
    assert fresh.extend(_in(5.0)) is True
    assert fresh.trip_if_expired() is False


@pytest.mark.asyncio
async def test_a_caller_arriving_after_a_shared_read_was_cut_gets_a_read_of_its_own(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    calls: list[int] = []
    unwinding = threading.Event()
    release = threading.Event()

    def operation(**_key):
        calls.append(1)
        if len(calls) > 1:
            return _busy_read(daemon._engine, 0.05)()
        try:
            return _busy_read(daemon._engine, 5.0)()
        except Exception:
            # Hold the cut read in its unwind, where a late caller can arrive.
            unwinding.set()
            release.wait(2)
            raise

    async def caller(deadline_ns: int):
        token = _REQUEST_DEADLINE_NS.set(deadline_ns)
        try:
            return await daemon._coalesced_session_detail_read("test.shared", operation, key="same")
        finally:
            _REQUEST_DEADLINE_NS.reset(token)

    try:
        first = asyncio.create_task(caller(_in(0.05)))
        assert await asyncio.to_thread(unwinding.wait, 2)
        # Joining the dying read would revive its deadline and lose the error;
        # the late caller must get a read of its own.
        late = await caller(_in(5.0))
        assert late == {"done": True}
        assert len(calls) == 2
        release.set()
        with pytest.raises(CatalogReadExpired):
            await first
    finally:
        release.set()
        await daemon.close()
