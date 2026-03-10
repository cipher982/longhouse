"""Tests for job registration warning capture and API exposure."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.jobs.registry import get_registration_warnings
from zerg.jobs.registry import register_all_jobs
from zerg.models.models import User

os.environ.setdefault("DATABASE_URL", "sqlite://")


def _make_db(tmp_path):
    """Create a SQLite DB for API-level tests."""
    db_path = tmp_path / "test_jobs_warnings.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(db_session):
    """Create a TestClient with auth + DB dependency overrides."""
    from zerg.dependencies.auth import require_admin
    from zerg.main import api_app
    from zerg.main import app

    admin = db_session.query(User).filter(User.email == "admin@local").first()
    if not admin:
        admin = User(email="admin@local", role="ADMIN")
        db_session.add(admin)
        db_session.commit()
        db_session.refresh(admin)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_require_admin():
        return admin

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[require_admin] = override_require_admin
    return TestClient(app, backend="asyncio"), api_app


@pytest.mark.asyncio
async def test_register_all_jobs_captures_import_failures(monkeypatch):
    """register_all_jobs should retain import/manifest failures for later inspection."""
    import importlib

    import zerg.jobs.registry as registry

    real_import_module = importlib.import_module
    imported_modules: list[str] = []

    def fake_import_module(module_name: str):
        imported_modules.append(module_name)
        if module_name == "zerg.jobs.daily_digest":
            raise ImportError("simulated daily_digest import failure")
        if module_name in {
            "zerg.jobs.reflection",
            "zerg.jobs.health_monitor",
            "zerg.jobs.check_stale_agents",
            "zerg.jobs.oikos_operator_sweep",
        }:
            return SimpleNamespace(__name__=module_name)
        return real_import_module(module_name)

    monkeypatch.setattr(registry.importlib, "import_module", fake_import_module)

    with (
        patch("zerg.jobs.registry.should_load_manifest_jobs", return_value=True),
        patch("zerg.jobs.loader.load_jobs_manifest", AsyncMock(return_value=None)),
    ):
        await register_all_jobs(scheduler=None)

    warnings = get_registration_warnings()
    assert any("zerg.jobs.daily_digest" in warning for warning in warnings)
    assert any("simulated daily_digest import failure" in warning for warning in warnings)
    assert "zerg.jobs.oikos_operator_sweep" in imported_modules


def test_list_jobs_includes_registration_warnings(tmp_path):
    """GET /api/jobs should include latest registration warnings in the response."""
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        client, api_app_ref = _make_client(db)
        try:
            with (
                patch("zerg.routers.jobs._ensure_jobs_registered", AsyncMock(return_value=None)),
                patch("zerg.routers.jobs.job_registry.list_jobs", return_value=[]),
                patch(
                    "zerg.routers.jobs.get_registration_warnings",
                    return_value=["Failed to import zerg.jobs.daily_digest"],
                ),
            ):
                response = client.get("/api/jobs")

            assert response.status_code == 200
            payload = response.json()
            assert payload["total"] == 0
            assert payload["jobs"] == []
            assert payload["registration_warnings"] == ["Failed to import zerg.jobs.daily_digest"]
        finally:
            api_app_ref.dependency_overrides = {}


@pytest.mark.asyncio
async def test_register_all_jobs_skips_manifest_without_external_config(monkeypatch):
    """Builtin-only mode should not try to execute a stale local jobs manifest."""
    import importlib

    import zerg.jobs.registry as registry

    real_import_module = importlib.import_module
    imported_modules: list[str] = []

    def fake_import_module(module_name: str):
        imported_modules.append(module_name)
        if module_name in {
            "zerg.jobs.daily_digest",
            "zerg.jobs.reflection",
            "zerg.jobs.health_monitor",
            "zerg.jobs.check_stale_agents",
            "zerg.jobs.oikos_operator_sweep",
        }:
            return SimpleNamespace(__name__=module_name)
        return real_import_module(module_name)

    monkeypatch.setattr(registry.importlib, "import_module", fake_import_module)
    load_manifest = AsyncMock(return_value=None)

    with (
        patch("zerg.jobs.registry.should_load_manifest_jobs", return_value=False),
        patch("zerg.jobs.loader.load_jobs_manifest", load_manifest),
    ):
        await register_all_jobs(scheduler=None)

    load_manifest.assert_not_awaited()
    assert get_registration_warnings() == []
    assert "zerg.jobs.oikos_operator_sweep" in imported_modules
