from __future__ import annotations

from fastapi import FastAPI
import pytest

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
