from __future__ import annotations

import pytest
from fastapi import FastAPI

import zerg.database as database_module
import zerg.lifespan as lifespan_module
import zerg.services.maintenance as maintenance_module


@pytest.mark.asyncio
async def test_live_catalog_lifespan_never_initializes_or_configures_archive(monkeypatch):
    calls: list[str] = []

    def fail_archive(*_args, **_kwargs):
        raise AssertionError("API process opened the cold archive")

    monkeypatch.setattr(lifespan_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(lifespan_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(lifespan_module, "initialize_database", fail_archive)
    monkeypatch.setattr(lifespan_module, "initialize_live_database", lambda: calls.append("live_init"))
    monkeypatch.setattr(lifespan_module, "configure_observability", lambda: None)
    monkeypatch.setattr(lifespan_module, "shutdown_observability", lambda: None)
    monkeypatch.setattr(lifespan_module, "_validate_models_config_startup", lambda: None)
    monkeypatch.setattr(lifespan_module._settings, "testing", True)
    monkeypatch.setattr(database_module, "configure_write_serializer", fail_archive)
    monkeypatch.setattr(database_module, "configure_live_write_serializer", lambda: calls.append("live_writer"))

    app = FastAPI()
    async with lifespan_module.lifespan(app):
        assert calls == ["live_init", "live_writer"]


@pytest.mark.asyncio
async def test_live_catalog_maintenance_never_opens_legacy_notification_database(monkeypatch):
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        database_module,
        "get_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("maintenance opened the legacy database")),
    )

    await maintenance_module._process_queued_notifications_once()


@pytest.mark.asyncio
async def test_production_live_catalog_lifespan_delegates_schema_to_catalogd(monkeypatch):
    calls: list[str] = []

    def forbidden_direct_schema_init(*_args, **_kwargs):
        raise AssertionError("production API process must not initialize the live schema")

    async def start_catalogd():
        calls.append("catalogd_start")
        return {"ready": True, "schema_version": 1}

    async def stop_catalogd():
        calls.append("catalogd_stop")

    async def start_searchd():
        calls.append("searchd_start")
        return None

    async def stop_searchd():
        calls.append("searchd_stop")

    def start_search_projector():
        calls.append("search_projector_start")
        return True

    async def stop_search_projector():
        calls.append("search_projector_stop")

    class StorageWorkers:
        def __init__(self, label):
            self.label = label

        async def start(self):
            calls.append(f"{self.label}_workers_start")

    class Runner:
        def start(self):
            calls.append("runner_start")

        def stop(self):
            calls.append("runner_stop")

    async def completed_loop():
        return None

    async def noop_async(*_args, **_kwargs):
        return None

    async def stop_raw_workers():
        calls.append("raw_workers_stop")

    async def stop_render_workers():
        calls.append("render_workers_stop")

    monkeypatch.setattr(lifespan_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(lifespan_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(lifespan_module, "initialize_live_database", forbidden_direct_schema_init)
    monkeypatch.setattr(lifespan_module, "configure_observability", lambda: None)
    monkeypatch.setattr(lifespan_module, "shutdown_observability", lambda: None)
    monkeypatch.setattr(lifespan_module, "_validate_models_config_startup", lambda: None)
    monkeypatch.setattr(lifespan_module, "_enforce_single_tenant_startup", lambda _app: None)
    monkeypatch.setattr(lifespan_module._settings, "testing", False)
    monkeypatch.setattr(database_module, "configure_live_write_serializer", lambda: calls.append("live_writer"))
    monkeypatch.setattr(database_module, "start_wal_checkpoint_loop", noop_async)
    monkeypatch.setattr(database_module, "stop_wal_checkpoint_loop", noop_async)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.start_catalogd_supervisor", start_catalogd)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.stop_catalogd_supervisor", stop_catalogd)
    monkeypatch.setattr("zerg.services.searchd_supervisor.start_searchd_supervisor", start_searchd)
    monkeypatch.setattr("zerg.services.searchd_supervisor.stop_searchd_supervisor", stop_searchd)
    monkeypatch.setattr("zerg.services.search_v2_projector.start_search_v2_projector", start_search_projector)
    monkeypatch.setattr("zerg.services.search_v2_projector.stop_search_v2_projector", stop_search_projector)
    monkeypatch.setattr("zerg.services.raw_object_workers.get_raw_object_worker_pool", lambda: StorageWorkers("raw"))
    monkeypatch.setattr("zerg.services.raw_object_workers.close_raw_object_worker_pool", stop_raw_workers)
    monkeypatch.setattr(
        "zerg.services.render_object_workers.get_render_object_worker_pool", lambda: StorageWorkers("render")
    )
    monkeypatch.setattr("zerg.services.render_object_workers.close_render_object_worker_pool", stop_render_workers)
    monkeypatch.setattr("zerg.services.live_control_catalog.run_live_catalog_input_recovery_loop", completed_loop)
    monkeypatch.setattr("zerg.services.maintenance.stop_maintenance_loop", noop_async)
    monkeypatch.setattr("zerg.services.retrieval_index_jobs.stop_recall_index_worker", noop_async)
    monkeypatch.setattr("zerg.utils.async_runner.get_shared_runner", lambda: Runner())
    monkeypatch.setattr("zerg.websocket.manager.topic_manager.shutdown", noop_async)
    monkeypatch.setattr(lifespan_module.ops_events_bridge, "stop", lambda: None)
    monkeypatch.setattr("zerg.tools.mcp_adapter.MCPManager.shutdown_stdio_processes", noop_async)

    app = FastAPI()
    async with lifespan_module.lifespan(app):
        assert calls[:7] == [
            "catalogd_start",
            "searchd_start",
            "raw_workers_start",
            "render_workers_start",
            "search_projector_start",
            "live_writer",
            "runner_start",
        ]
        assert app.state.catalogd_ping["ready"] is True
        assert app.state.searchd_ping is None

    assert calls[-6:] == [
        "runner_stop",
        "search_projector_stop",
        "raw_workers_stop",
        "render_workers_stop",
        "searchd_stop",
        "catalogd_stop",
    ]


@pytest.mark.asyncio
async def test_lifespan_stops_searchd_and_catalogd_when_later_startup_fails(monkeypatch):
    calls: list[str] = []

    async def start_catalogd():
        calls.append("catalogd_start")
        return {"ready": True}

    async def stop_catalogd():
        calls.append("catalogd_stop")

    async def start_searchd():
        calls.append("searchd_start")
        return {"ready": True}

    async def stop_searchd():
        calls.append("searchd_stop")

    class FailingWorkers:
        def __init__(self, label: str, *, fail: bool = False):
            self.label = label
            self.fail = fail

        async def start(self):
            calls.append(f"{self.label}_start")
            if self.fail:
                raise RuntimeError("synthetic worker startup failure")

    async def stop_raw():
        calls.append("raw_stop")

    async def stop_render():
        calls.append("render_stop")

    monkeypatch.setattr(lifespan_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(lifespan_module, "configure_observability", lambda: None)
    monkeypatch.setattr(lifespan_module._settings, "testing", False)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.start_catalogd_supervisor", start_catalogd)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.stop_catalogd_supervisor", stop_catalogd)
    monkeypatch.setattr("zerg.services.searchd_supervisor.start_searchd_supervisor", start_searchd)
    monkeypatch.setattr("zerg.services.searchd_supervisor.stop_searchd_supervisor", stop_searchd)
    monkeypatch.setattr(
        "zerg.services.raw_object_workers.get_raw_object_worker_pool",
        lambda: FailingWorkers("raw", fail=True),
    )
    monkeypatch.setattr(
        "zerg.services.render_object_workers.get_render_object_worker_pool",
        lambda: FailingWorkers("render"),
    )
    monkeypatch.setattr("zerg.services.raw_object_workers.close_raw_object_worker_pool", stop_raw)
    monkeypatch.setattr("zerg.services.render_object_workers.close_render_object_worker_pool", stop_render)

    with pytest.raises(RuntimeError, match="synthetic worker startup failure"):
        async with lifespan_module.lifespan(FastAPI()):
            pass

    assert calls[:2] == ["catalogd_start", "searchd_start"]
    assert calls[-4:] == ["raw_stop", "render_stop", "searchd_stop", "catalogd_stop"]
