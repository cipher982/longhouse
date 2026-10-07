"""Warm candidate start (B2): permit, bound port, catalog lock, held requests."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.lifespan as lifespan_module
import zerg.services.catalog_handoff as handoff_module
from zerg.middleware.runtime_write_admission import RuntimeWriteAdmissionMiddleware
from zerg.services.catalog_handoff import CatalogHandoff
from zerg.services.catalog_handoff import CatalogHandoffAborted
from zerg.services.runtime_admission import RuntimeAdmission


def _handoff(tmp_path: Path, *, cutoff: datetime | None = None) -> CatalogHandoff:
    return CatalogHandoff(directory=tmp_path / "handoff", attempt_id="a-1", runtime_epoch="epoch-1", cutoff=cutoff)


def _permit(handoff: CatalogHandoff, attempt_id: str = "a-1") -> None:
    handoff.marker_path("permit").write_text(json.dumps({"attempt_id": attempt_id}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_handoff():
    handoff_module.reset_catalog_handoff_for_tests(None)
    yield
    handoff_module.reset_catalog_handoff_for_tests(None)


@pytest.mark.asyncio
async def test_warm_marker_then_waits_for_its_own_attempts_permit(tmp_path) -> None:
    handoff = _handoff(tmp_path)
    waiter = asyncio.create_task(handoff.wait_for_permit())
    await asyncio.sleep(0.05)
    warm = json.loads(handoff.marker_path("warm").read_text())
    assert warm["state"] == "warm" and warm["runtime_epoch"] == "epoch-1" and warm["attempt_id"] == "a-1"
    assert not waiter.done()

    _permit(handoff, attempt_id="a-other")
    await asyncio.sleep(0.05)
    assert not waiter.done(), "a permit for another attempt must not release this candidate"

    _permit(handoff)
    await asyncio.wait_for(waiter, timeout=1)
    assert "permit" in handoff.timings


@pytest.mark.asyncio
async def test_permit_wait_ends_at_the_attempt_cutoff(tmp_path) -> None:
    handoff = _handoff(tmp_path, cutoff=datetime.now(UTC) + timedelta(seconds=0.05))
    with pytest.raises(CatalogHandoffAborted):
        await asyncio.wait_for(handoff.wait_for_permit(), timeout=1)


def test_lock_probe_never_keeps_the_catalog_lock(tmp_path) -> None:
    lock_path = tmp_path / "catalog.db.catalogd.lock"
    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert handoff_module.lock_is_free(lock_path) is False
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    assert handoff_module.lock_is_free(lock_path) is True
    # catalogd takes the lock itself on its own descriptor; the probe never keeps it.
    with lock_path.open("a+") as again:
        fcntl.flock(again.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(again.fileno(), fcntl.LOCK_UN)


def test_catalogd_handoff_wait_needs_the_permit_and_then_the_free_lock(monkeypatch, tmp_path) -> None:
    import threading

    directory = tmp_path / "handoff"
    directory.mkdir()
    lock_path = tmp_path / "live.db.catalogd.lock"
    monkeypatch.setenv("LONGHOUSE_CATALOG_HANDOFF_DIR", str(directory))
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_ATTEMPT_ID", "a-1")
    monkeypatch.setenv("LONGHOUSE_CLAIM_CUTOFF", (datetime.now(UTC) + timedelta(seconds=5)).isoformat())
    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    done = threading.Event()
    errors: list[BaseException] = []

    def wait() -> None:
        try:
            handoff_module.wait_for_handoff_from_env(lock_path)
        except BaseException as exc:  # surfaced below instead of a bare timeout
            errors.append(exc)
        done.set()

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        assert not done.wait(0.1)
        (directory / "a-1.permit").write_text(json.dumps({"attempt_id": "a-1"}))
        assert not done.wait(0.1), "the permit alone must not open a catalog the predecessor still holds"
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        assert done.wait(2)
        assert errors == []
    finally:
        holder.close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_bound_marker_follows_a_listening_port(tmp_path) -> None:
    handoff = _handoff(tmp_path)
    handoff.directory.mkdir(parents=True)
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await handoff.announce_bound(port)
    finally:
        server.close()
        await server.wait_closed()
    assert json.loads(handoff.marker_path("bound").read_text())["state"] == "bound"


def test_handoff_requires_a_pending_attempt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LONGHOUSE_CATALOG_HANDOFF_DIR", str(tmp_path))
    monkeypatch.delenv("LONGHOUSE_DEPLOYMENT_PENDING", raising=False)
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_ATTEMPT_ID", "a-1")
    with pytest.raises(RuntimeError):
        handoff_module.catalog_handoff()


def test_no_handoff_dir_is_an_ordinary_start(monkeypatch) -> None:
    monkeypatch.delenv("LONGHOUSE_CATALOG_HANDOFF_DIR", raising=False)
    assert handoff_module.catalog_handoff() is None
    assert handoff_module.catalog_handoff_pending() is None


def _restarted_candidate_env(monkeypatch, tmp_path, *, cutoff: datetime) -> None:
    monkeypatch.setenv("LONGHOUSE_CATALOG_HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_ATTEMPT_ID", "a-1")
    monkeypatch.setenv("LONGHOUSE_CLAIM_CUTOFF", cutoff.isoformat().replace("+00:00", "Z"))


def test_a_restart_after_the_attempt_cutoff_is_an_ordinary_start(monkeypatch, tmp_path) -> None:
    # A cut-over container keeps the candidate environment; no permit will come
    # for an attempt past its cutoff, so waiting for one would fail every restart.
    _restarted_candidate_env(monkeypatch, tmp_path, cutoff=datetime.now(UTC) - timedelta(seconds=1))
    assert handoff_module.catalog_handoff() is None
    assert handoff_module.catalog_handoff_pending() is None


@pytest.mark.asyncio
async def test_a_restart_inside_the_cutoff_passes_through_the_kept_permit(monkeypatch, tmp_path) -> None:
    _restarted_candidate_env(monkeypatch, tmp_path, cutoff=datetime.now(UTC) + timedelta(minutes=5))
    handoff = handoff_module.catalog_handoff()
    assert handoff is not None
    handoff.directory.mkdir(parents=True)
    _permit(handoff)
    await asyncio.wait_for(handoff.wait_for_permit(), timeout=1.0)


@pytest.mark.asyncio
async def test_request_on_a_warm_candidate_is_held_until_its_first_reopen(monkeypatch, tmp_path) -> None:
    from zerg.services import runtime_admission as admission_module

    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    runtime = RuntimeAdmission()
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    handoff = _handoff(tmp_path)
    handoff.directory.mkdir(parents=True)
    handoff_module.reset_catalog_handoff_for_tests(handoff)
    served: list[str] = []

    async def app(scope, receive, send):
        served.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = RuntimeWriteAdmissionMiddleware(app)
    task = asyncio.create_task(middleware({"type": "http", "method": "GET", "path": "/api/timeline/sessions"}, receive, send))
    await asyncio.sleep(0.05)
    assert served == [], "a read must not reach the app before the candidate's own catalog is open"

    # Internal deployment control is never held: readiness answers for itself.
    await middleware({"type": "http", "method": "GET", "path": "/api/internal/deployments/a-1/readiness"}, receive, send)
    assert served == ["/api/internal/deployments/a-1/readiness"]

    handoff.mark_ready()
    await asyncio.sleep(0.05)
    assert served == ["/api/internal/deployments/a-1/readiness"], "the deployer's probe and reopen go first"

    runtime._initial_open_event.set()
    await asyncio.wait_for(task, timeout=1)
    assert served[-1] == "/api/timeline/sessions"

    # Once this process has opened, a later drain of it never holds reads.
    runtime._state = "draining"
    await middleware({"type": "http", "method": "GET", "path": "/api/timeline/sessions"}, receive, send)
    assert served[-1] == "/api/timeline/sessions" and len(served) == 3


@pytest.mark.asyncio
async def test_held_request_gets_a_typed_retryable_503_when_the_catalog_never_opens(monkeypatch, tmp_path) -> None:
    from zerg.middleware import runtime_write_admission as middleware_module
    from zerg.services import runtime_admission as admission_module

    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    runtime = RuntimeAdmission()
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(middleware_module, "_CATALOG_HANDOFF_HOLD_SECONDS", 0.05)
    handoff = _handoff(tmp_path)
    handoff.directory.mkdir(parents=True)
    handoff_module.reset_catalog_handoff_for_tests(handoff)

    async def app(scope, receive, send):
        raise AssertionError("must not reach the app")

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    await RuntimeWriteAdmissionMiddleware(app)({"type": "http", "method": "POST", "path": "/api/agents/heartbeat"}, receive, send)
    assert sent[0]["status"] == 503
    body = json.loads(sent[1]["body"])
    assert body["code"] == "runtime_restarting" and body["retryable"] is True
    assert body["admission"] == "pending"


def _readiness_route(monkeypatch, runtime: RuntimeAdmission):
    from zerg.routers import internal_deployments

    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(internal_deployments, "get_settings", lambda: SimpleNamespace(internal_api_secret="secret"))
    monkeypatch.setattr(internal_deployments, "_signal_runtime_lifecycle", AsyncMock())
    monkeypatch.setattr(internal_deployments, "_HANDOFF_READINESS_WAIT_SECONDS", 0.05)

    def no_catalog_ping():
        raise AssertionError("readiness must not ping the shared catalog socket before the handoff")

    monkeypatch.setattr(internal_deployments, "_runtime_evidence", no_catalog_ping)
    return internal_deployments


def _candidate_env(monkeypatch) -> None:
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_PENDING", "1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_ATTEMPT_ID", "a-1")
    monkeypatch.setenv("LONGHOUSE_DEPLOYMENT_GENERATION", "7")


@pytest.mark.asyncio
async def test_readiness_is_not_ready_until_the_handoff_and_conflict_after_failure(monkeypatch, tmp_path) -> None:
    _candidate_env(monkeypatch)
    runtime = RuntimeAdmission()
    routes = _readiness_route(monkeypatch, runtime)
    handoff = _handoff(tmp_path)
    handoff.directory.mkdir(parents=True)
    handoff_module.reset_catalog_handoff_for_tests(handoff)

    response = await routes.runtime_readiness("a-1", "7", None, None, None, "secret")
    assert response.status_code == 503
    assert json.loads(response.body)["outcome"] == "not_ready"

    handoff.mark_failed("catalogd did not become ready")
    response = await routes.runtime_readiness("a-1", "7", None, None, None, "secret")
    assert response.status_code == 409
    assert "catalog handoff failed" in json.loads(response.body)["detail"]


@pytest.mark.asyncio
async def test_drained_process_releases_the_catalog_before_its_other_shutdown_steps(monkeypatch) -> None:
    calls: list[str] = []
    runtime = RuntimeAdmission()
    runtime._state = "drained"
    monkeypatch.setattr("zerg.services.runtime_admission.runtime_admission", lambda: runtime)
    monkeypatch.setattr(lifespan_module._settings, "testing", False)

    async def record(name):
        calls.append(name)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("zerg.services.catalogd_supervisor.stop_catalogd_supervisor", lambda: record("catalogd"))
    monkeypatch.setattr("zerg.services.searchd_supervisor.stop_searchd_supervisor", lambda: record("searchd"))
    monkeypatch.setattr("zerg.database.stop_wal_checkpoint_loop", lambda: record("wal"))
    monkeypatch.setattr("zerg.services.maintenance.stop_maintenance_loop", noop)
    monkeypatch.setattr("zerg.websocket.manager.topic_manager.shutdown", noop)
    monkeypatch.setattr("zerg.services.raw_object_workers.close_raw_object_worker_pool", lambda: record("raw"))
    monkeypatch.setattr("zerg.services.render_object_workers.close_render_object_worker_pool", noop)
    monkeypatch.setattr("zerg.services.semantic_v2_projector.stop_semantic_v2_projector", noop)
    monkeypatch.setattr("zerg.services.search_v2_projector.stop_search_v2_projector", noop)
    monkeypatch.setattr("zerg.services.embeddings_v2_projector.stop_embeddings_v2_projector", noop)
    monkeypatch.setattr("zerg.services.local_embedder.stop_local_embedder_initialization", noop)
    monkeypatch.setattr("zerg.services.storage_session_titles.stop_storage_title_workers", noop)

    from fastapi import FastAPI

    await lifespan_module._stop_runtime_services(FastAPI(), 0.0, owns_test_catalog=False)
    assert calls[0] == "catalogd"
    assert calls.count("catalogd") == 1
    assert "searchd" in calls and "raw" in calls

    calls.clear()
    runtime._state = "open"
    await lifespan_module._stop_runtime_services(FastAPI(), 0.0, owns_test_catalog=False)
    assert calls[-1] == "catalogd", "an ordinary stop keeps the catalog until last"


@pytest.mark.asyncio
async def test_warm_lifespan_binds_only_after_the_permit_and_starts_services_after_catalogd(monkeypatch, tmp_path) -> None:
    handoff = _handoff(tmp_path)
    handoff_module.reset_catalog_handoff_for_tests(handoff)
    monkeypatch.setattr(lifespan_module._settings, "testing", False)
    monkeypatch.setattr(lifespan_module, "get_settings", lambda: lifespan_module._settings)
    monkeypatch.setattr("zerg.services.event_loop_lag.start_deploy_window_monitor", lambda _done: None)
    monkeypatch.setattr(lifespan_module, "_preload_catalog_dependent_modules", lambda: None)
    started: list[object] = []
    stopped: list[str] = []
    catalogd_up = asyncio.Event()
    supervisor_calls: list[dict] = []

    async def start_catalogd(**kwargs):
        supervisor_calls.append(kwargs)
        await catalogd_up.wait()
        return {"ready": True}

    async def start_services(app, startup_started, *, owns_test_catalog, e2e_catalog, catalogd_ping=None, defer_background_loops=False):
        assert defer_background_loops is True, "a warm candidate starts its background loops after reopen"
        started.append(catalogd_ping)

    async def stop_services(app, shutdown_started, *, owns_test_catalog):
        stopped.append("services")

    async def bound(_port):
        handoff._write_marker("bound", {"state": "bound"})

    monkeypatch.setattr("zerg.services.catalogd_supervisor.start_catalogd_supervisor", start_catalogd)
    monkeypatch.setattr(lifespan_module, "_start_runtime_services", start_services)
    monkeypatch.setattr(lifespan_module, "_stop_runtime_services", stop_services)
    monkeypatch.setattr(handoff, "announce_bound", bound)

    from fastapi import FastAPI

    app = FastAPI()
    context = lifespan_module.lifespan(app)
    entered = asyncio.create_task(context.__aenter__())
    await asyncio.sleep(0.05)
    assert supervisor_calls and supervisor_calls[0]["handoff"] is True, "catalogd is spawned before the permit"
    assert not entered.done(), "HTTP must not bind before the permit"
    _permit(handoff)
    await asyncio.wait_for(entered, timeout=1)
    await asyncio.sleep(0.05)
    assert started == [] and not handoff.ready.is_set()

    catalogd_up.set()
    await asyncio.wait_for(handoff.ready.wait(), timeout=1)
    assert started == [{"ready": True}], "startup reuses the pre-spawned catalogd instead of starting another"
    assert json.loads(handoff.marker_path("catalog").read_text())["state"] == "catalog_ready"
    await context.__aexit__(None, None, None)
    assert stopped == ["services"]
    import gc

    gc.unfreeze()  # the warm path froze this test process's heap


@pytest.mark.asyncio
async def test_evidence_is_not_ready_while_the_handoff_is_pending(monkeypatch, tmp_path) -> None:
    _candidate_env(monkeypatch)
    runtime = RuntimeAdmission()
    routes = _readiness_route(monkeypatch, runtime)
    handoff = _handoff(tmp_path)
    handoff.directory.mkdir(parents=True)
    handoff_module.reset_catalog_handoff_for_tests(handoff)
    response = await routes.runtime_evidence("secret")
    assert response.status_code == 503
    assert json.loads(response.body)["outcome"] == "not_ready"


@pytest.mark.asyncio
async def test_a_stream_opened_on_a_draining_process_ends_with_the_lifecycle(monkeypatch) -> None:
    from zerg.services import runtime_admission as admission_module

    runtime = RuntimeAdmission()
    runtime._state = "drained"
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)

    async def app(scope, receive, send):
        raise AssertionError("a drained process must not open a new long-lived stream")

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/api/agents/sessions/stream", "headers": [(b"accept", b"text/event-stream")]}
    await RuntimeWriteAdmissionMiddleware(app)(scope, receive, send)
    assert sent[0]["status"] == 200
    assert sent[1]["body"].startswith(b"event: host_lifecycle\ndata: ")

    runtime._state = "open"
    served: list[str] = []

    async def serving_app(scope, receive, send):
        served.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await RuntimeWriteAdmissionMiddleware(serving_app)(scope, receive, send)
    assert served == ["/api/agents/sessions/stream"]


@pytest.mark.asyncio
async def test_drain_post_answers_drained_once_writers_finish_instead_of_draining(monkeypatch) -> None:
    from zerg.routers import internal_deployments

    runtime = RuntimeAdmission()
    monkeypatch.setattr(internal_deployments, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(internal_deployments, "get_settings", lambda: SimpleNamespace(internal_api_secret="secret"))
    monkeypatch.setattr(internal_deployments, "_signal_runtime_lifecycle", AsyncMock())
    depths = [1, 1, 1, 0]

    async def catalog_probe(_operation: str) -> dict[str, object]:
        depth = depths.pop(0) if depths else 0
        return {"available": True, "state": "closed", "depth": depth, "accepting": False, "active_label": None}

    monkeypatch.setattr(internal_deployments, "_catalog_admission_probe", catalog_probe)
    body = internal_deployments.DeploymentFenceRequest(
        request_id="drain-request",
        deployment_id="deployment-1",
        target_id="target-1",
        generation="7",
        deadline_utc=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        grace_seconds=0,
    )
    response = await internal_deployments.drain_runtime("attempt-1", body, "secret")
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["state"] == "drained"
    assert payload["request_id"] == "drain-request" and payload["attempt_id"] == "attempt-1"


def test_only_api_stream_routes_are_answered_by_the_drain_lifecycle() -> None:
    from zerg.middleware.runtime_write_admission import _wants_event_stream

    accept = [(b"accept", b"text/event-stream")]
    assert _wants_event_stream({"method": "GET", "path": "/api/agents/sessions/stream", "headers": accept})
    assert _wants_event_stream({"method": "GET", "path": "/api/telemetry/canary-stream", "headers": accept})
    assert not _wants_event_stream({"method": "GET", "path": "/api/agents/sessions", "headers": accept})
    assert not _wants_event_stream({"method": "GET", "path": "/admin", "headers": accept})
    assert not _wants_event_stream({"method": "GET", "path": "/api/agents/sessions/stream", "headers": []})


@pytest.mark.asyncio
async def test_a_failed_handoff_stops_the_pre_spawned_catalogd(monkeypatch, tmp_path) -> None:
    handoff = _handoff(tmp_path)
    handoff.directory.mkdir(parents=True)
    stopped: list[str] = []

    async def bound_fails(_port):
        raise CatalogHandoffAborted("runtime HTTP did not bind after the handoff permit")

    async def stop_catalogd():
        stopped.append("catalogd")

    monkeypatch.setattr(handoff, "announce_bound", bound_fails)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.stop_catalogd_supervisor", stop_catalogd)
    from fastapi import FastAPI

    app = FastAPI()
    app.state.catalogd_start_task = asyncio.create_task(asyncio.Event().wait())
    await lifespan_module._complete_catalog_handoff(app, handoff, 0.0)
    assert handoff.failed is not None
    assert app.state.catalogd_start_task.cancelled()
    assert stopped == ["catalogd"]
