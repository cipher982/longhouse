from __future__ import annotations

import pytest
from fastapi import FastAPI

import zerg.database as database_module
import zerg.lifespan as lifespan_module


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
async def test_production_live_catalog_lifespan_delegates_schema_to_catalogd(monkeypatch):
    calls: list[str] = []

    def forbidden_direct_schema_init(*_args, **_kwargs):
        raise AssertionError("production API process must not initialize the live schema")

    async def start_catalogd():
        calls.append("catalogd_start")
        return {"ready": True, "schema_version": 1}

    async def stop_catalogd():
        calls.append("catalogd_stop")

    class RawWorkers:
        async def start(self):
            calls.append("raw_workers_start")

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
    monkeypatch.setattr("zerg.services.raw_object_workers.get_raw_object_worker_pool", lambda: RawWorkers())
    monkeypatch.setattr("zerg.services.raw_object_workers.close_raw_object_worker_pool", stop_raw_workers)
    monkeypatch.setattr("zerg.services.archive_worker_supervisor.start_archive_worker_supervisor", lambda: None)
    monkeypatch.setattr("zerg.services.archive_worker_supervisor.stop_archive_worker_supervisor", noop_async)
    monkeypatch.setattr("zerg.services.live_control_catalog.run_live_catalog_input_recovery_loop", completed_loop)
    monkeypatch.setattr("zerg.services.maintenance.stop_maintenance_loop", noop_async)
    monkeypatch.setattr("zerg.services.retrieval_index_jobs.stop_recall_index_worker", noop_async)
    monkeypatch.setattr("zerg.utils.async_runner.get_shared_runner", lambda: Runner())
    monkeypatch.setattr("zerg.websocket.manager.topic_manager.shutdown", noop_async)
    monkeypatch.setattr(lifespan_module.ops_events_bridge, "stop", lambda: None)
    monkeypatch.setattr("zerg.tools.mcp_adapter.MCPManager.shutdown_stdio_processes", noop_async)

    app = FastAPI()
    async with lifespan_module.lifespan(app):
        assert calls[:4] == ["catalogd_start", "raw_workers_start", "live_writer", "runner_start"]
        assert app.state.catalogd_ping["ready"] is True

    assert calls[-3:] == ["runner_stop", "raw_workers_stop", "catalogd_stop"]
