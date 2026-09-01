"""Hot routes under a saturated writer, on the path a Runtime Host takes.

This file used to describe an architecture the Runtime Host no longer runs. It
configured two SQLAlchemy ``WriteSerializer`` instances, wrote ``LiveHeartbeatStamp``
and ``LiveRuntimeState`` through the live one, and finished by draining
``LiveArchiveOutbox`` into the archive database. ``lifespan`` configures neither
serializer once ``live_catalog_enabled()`` is true, and the outbox drain has since
been deleted outright -- ``LiveArchiveOutbox`` is a receipt table now, not a queue.
Those rows are still real; catalogd owns them, and the API process reaches them
over a Unix socket.

The subject survives the move, because the writer it was about survives it. A
catalog has exactly one writer thread and a bounded pool of interactive read
workers, and the failure that split exists to prevent is the one
``CatalogWriterStats`` records: a request that waits behind bulk ingest is
indistinguishable from an unreachable host. So the saturated writer here is the
one production actually has -- a real ``storage.raw_object.commit.v2`` holding
the single catalogd writer thread -- and the hot routes are asked the same two
questions as before: do reads answer while it is held, and does the archive
database stay shut.
"""

from __future__ import annotations

import functools
import threading
import time
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import event

import zerg.data_plane as data_plane_module
import zerg.database as database_module
from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.catalogd.store import CatalogStore

OWNER_EMAIL = "owner@hot-path.test"
DEVICE_ID = "cinder"
PROJECT = "longhouse"
ROUTE_TIMEOUT_SECONDS = 5.0
# Only a backstop: a writer nobody releases fails the test instead of hanging
# the suite behind it.
WRITER_HOLD_TIMEOUT_SECONDS = 30.0
HEARTBEAT_BODY: dict[str, Any] = {
    "version": "0.5.0",
    "daemon_pid": 12345,
    "disk_free_bytes": 50_000_000_000,
}


@pytest.fixture()
def live() -> Iterator[LiveCatalog]:
    """A Runtime Host shaped the way production shapes one."""

    with provision_live_catalog() as catalog:
        yield catalog


@pytest.fixture()
def client(live: LiveCatalog):
    """``api_app`` with its import-time db dependencies forced to production's."""

    with live.http_client() as test_client:
        yield test_client


class _Call:
    """One HTTP call made from another thread, with its outcome kept."""

    def __init__(self, run) -> None:
        self._run = run
        self.response: Any = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._body, name="hot-route-call", daemon=True)

    def _body(self) -> None:
        try:
            self.response = self._run()
        except BaseException as exc:  # surfaced by result()
            self.error = exc

    def start(self) -> "_Call":
        self._thread.start()
        return self

    @property
    def returned(self) -> bool:
        return self.response is not None or self.error is not None

    def result(self, *, timeout: float = ROUTE_TIMEOUT_SECONDS):
        self._thread.join(timeout=timeout)
        assert not self._thread.is_alive(), "the hot route never returned"
        if self.error is not None:
            raise self.error
        return self.response


def _wait_until(predicate, *, otherwise: str, timeout: float = ROUTE_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{otherwise} within {timeout}s")


def _writer_admission(live: LiveCatalog) -> dict[str, Any]:
    """What catalogd says about its own writer, from the real ping."""

    return live.rpc("ping.v2")["writer_admission"]


@contextmanager
def _archive_database_use() -> Iterator[list[str]]:
    """Every use of the archive database inside the block, recorded.

    There are two ways a route can reach it and both are covered: taking a
    connection out of the engine ``zerg.database`` built at import, and, when
    this process has no such engine yet, building one from the environment on
    demand. Whichever it is, no hot route may do it.
    """

    engine = database_module.default_engine
    used: list[str] = []

    def _record(*_args) -> None:
        used.append(f"a connection was checked out of {engine.url}")

    if engine is not None:
        event.listen(engine, "checkout", _record)
    try:
        yield used
        if database_module.default_engine is not engine:
            used.append("an archive engine was built on demand")
    finally:
        if engine is not None:
            event.remove(engine, "checkout", _record)


@contextmanager
def _writer_held_by_bulk_ingest(live: LiveCatalog, *, owner_id: int) -> Iterator[None]:
    """Hold the single catalogd writer with a real transcript commit.

    Nothing is faked away: the commit is the one the Machine Agent ships, it
    runs on catalogd's writer thread, and it finishes for real once released.
    The only instrumentation is where inside that commit the thread waits.
    """

    entered = threading.Event()
    release = threading.Event()
    original = CatalogStore.commit_raw_object

    @functools.wraps(original)
    def _blocking_commit(self, **params):
        entered.set()
        assert release.wait(WRITER_HOLD_TIMEOUT_SECONDS), "the held catalog writer was never released"
        return original(self, **params)

    committed: list[Any] = []
    failed: list[BaseException] = []

    def _commit() -> None:
        try:
            committed.append(live.commit_session(owner_id=owner_id, project=PROJECT, texts=("bulk ingest",)))
        except BaseException as exc:
            failed.append(exc)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(CatalogStore, "commit_raw_object", _blocking_commit)
        worker = threading.Thread(target=_commit, name="bulk-ingest", daemon=True)
        worker.start()
        try:
            assert entered.wait(ROUTE_TIMEOUT_SECONDS), "the bulk commit never reached the catalog writer"
            yield
        finally:
            release.set()
            worker.join(timeout=WRITER_HOLD_TIMEOUT_SECONDS)
    assert not failed, failed[0]
    assert committed, "the held bulk commit never finished"


def test_hot_reads_answer_while_the_catalog_writer_is_held_by_bulk_ingest(live: LiveCatalog, client):
    """Reads keep their admission while one bulk write owns the writer.

    catalogd runs its interactive reads on a pool that is deliberately separate
    from the single writer thread. If a read ever crossed onto the writer, a
    transcript commit would take the timeline down with it -- the outage that
    was once misread as an unreachable host.
    """

    owner = live.create_user(OWNER_EMAIL)
    token = live.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    seeded = live.commit_session(owner_id=owner, project=PROJECT)
    headers = {"X-Agents-Token": token}

    with _writer_held_by_bulk_ingest(live, owner_id=owner):
        held = _writer_admission(live)
        assert held["active_label"] == "commit_raw_object"
        assert held["depth"] >= 1

        sessions = client.get("/agents/sessions", params={"limit": 20, "days_back": 7}, headers=headers)
        machines = client.get("/agents/machines", headers=headers)

        # Logical rather than wall-clock: the writer that was held before the
        # reads is still the same held writer afterwards, so neither read can
        # have waited for it.
        assert _writer_admission(live)["active_label"] == "commit_raw_object"

        assert sessions.status_code == 200, sessions.text
        assert str(seeded.session_id) in {row["id"] for row in sessions.json()["sessions"]}
        assert machines.status_code == 200, machines.text
        assert [row["device_id"] for row in machines.json()["machines"]] == [DEVICE_ID]

        # The hot write does share that writer, and the contract is that it
        # queues for it rather than being turned away.
        heartbeat = _Call(lambda: client.post("/agents/heartbeat", json=HEARTBEAT_BODY, headers=headers)).start()
        _wait_until(
            lambda: _writer_admission(live)["depth"] >= 2,
            otherwise="the hot heartbeat never queued behind the held writer",
        )
        queued = _writer_admission(live)
        assert queued["active_label"] == "commit_raw_object"
        assert queued["rejected_busy"] == 0
        assert not heartbeat.returned, "the hot write answered without reaching the writer it shares"

    assert heartbeat.result().status_code == 204

    health = client.get("/agents/machines/health", headers=headers)
    assert health.status_code == 200, health.text
    reported = health.json()["machines"]
    assert [row["device_id"] for row in reported] == [DEVICE_ID]
    assert reported[0]["version"] == HEARTBEAT_BODY["version"]


def test_hot_writes_reach_the_catalog_without_opening_the_archive_database(
    live: LiveCatalog,
    client,
    monkeypatch,
):
    """Heartbeat, runtime observation and managed launch are catalogd's, whole.

    Each of these used to be written here through a SQLAlchemy serializer and
    read back out of a live-store table. On a Runtime Host every one of them is
    an RPC, and the archive database is not opened at all -- so the guard is no
    longer "the request pool went back to zero" but "there was never a pool".
    """

    def _archive_store_unavailable(*_args, **_kwargs):
        raise AssertionError("hot heartbeat/runtime/launch paths must not open derived/archive stores")

    monkeypatch.setattr(data_plane_module, "create_archive_store", _archive_store_unavailable)

    owner = live.create_user(OWNER_EMAIL)
    token = live.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    seeded = live.commit_session(owner_id=owner, project=PROJECT)
    headers = {"X-Agents-Token": token}

    with _archive_database_use() as archive_use:
        heartbeat = client.post("/agents/heartbeat", json=HEARTBEAT_BODY, headers=headers)
        assert heartbeat.status_code == 204, heartbeat.text

        # The stamp comes back through the route an operator reads, out of the
        # catalog's copy of it -- not a table this process can open.
        health = client.get("/agents/machines/health", headers=headers)
        assert health.status_code == 200, health.text
        reported = health.json()["machines"]
        assert [row["device_id"] for row in reported] == [DEVICE_ID]
        assert reported[0]["version"] == HEARTBEAT_BODY["version"]

        runtime_key = f"codex:{seeded.session_id}"
        observation = {
            "runtime_key": runtime_key,
            "session_id": str(seeded.session_id),
            "provider": "codex",
            "device_id": DEVICE_ID,
            "source": "codex_bridge",
            "kind": "phase_signal",
            "phase": "running",
            "tool_name": "Shell",
            "occurred_at": datetime.now(UTC).isoformat(),
            "freshness_ms": 60_000,
            "dedupe_key": "hot-runtime-1",
            "payload": {},
        }
        runtime = client.post("/agents/runtime/events/batch", json={"events": [observation]}, headers=headers)
        assert runtime.status_code == 200, runtime.text
        assert runtime.json()["updated_runtime_keys"] == [runtime_key]

        # The live lane always answers accepted=len(batch), so the evidence that
        # anything persisted is which keys moved. Reshipping the same observation --
        # the engine's ordinary retry -- moves nothing, because the reducer compares
        # it against runtime state catalogd is already holding.
        replayed = client.post("/agents/runtime/events/batch", json={"events": [observation]}, headers=headers)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["updated_runtime_keys"] == []

        # And a later observation does move it, so the empty answer above is a
        # comparison against stored state rather than a route that stopped working.
        advanced = {
            **observation,
            "phase": "idle",
            "tool_name": None,
            "occurred_at": datetime.now(UTC).isoformat(),
            "dedupe_key": "hot-runtime-2",
        }
        progressed = client.post("/agents/runtime/events/batch", json={"events": [advanced]}, headers=headers)
        assert progressed.status_code == 200, progressed.text
        assert progressed.json()["updated_runtime_keys"] == [runtime_key]

        launch_body = {
            "session_id": str(uuid4()),
            "cwd": "/Users/me/repo",
            "provider": "codex",
            "project": PROJECT,
            "git_branch": "main",
            "machine_name": DEVICE_ID,
        }
        launch = client.post("/sessions/managed-local/this-device", json=launch_body, headers=headers)
        assert launch.status_code == 200, launch.text
        launched = launch.json()
        assert launched["session_id"] == launch_body["session_id"]
        assert launched["provider"] == "codex"
        assert "codex-bridge attach --session-id" in launched["attach_command"]
        assert launched["run_id"]

        # Same shape of readback for the launch: a client-minted identity that
        # reaches catalogd twice is one launch, and the second answer is the row the
        # first one wrote.
        relaunch = client.post("/sessions/managed-local/this-device", json=launch_body, headers=headers)
        assert relaunch.status_code == 200, relaunch.text
        assert relaunch.json()["run_id"] == launched["run_id"]

    assert archive_use == [], "a hot route reached the archive database"
