"""Product health checks over persisted session observations."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

import zerg.services.catalog_read_gateway as catalog_read_gateway
import zerg.services.product_health as product_health
import zerg.services.storage_session_titles as storage_session_titles
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app

PINNED_NOW = datetime(2026, 5, 22, 18, 0, 0, tzinfo=timezone.utc)


def _titles_on(monkeypatch):
    """A host where title generation can run, so its dependency health is graded."""

    monkeypatch.setattr(storage_session_titles, "title_generation_off_reason", lambda: None)


def _make_db(tmp_path):
    db_path = tmp_path / "test_product_health.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_client(SessionLocal):
    def override_get_db():
        with SessionLocal() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1,
        email="owner@example.com",
        role="USER",
    )
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def test_product_health_exposes_durable_session_title_dependency_incident(tmp_path, monkeypatch):
    _titles_on(monkeypatch)
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(
        catalog_read_gateway,
        "title_dependency_health",
        lambda: {
            "status": "degraded",
            "open_dependencies": 1,
            "blocked_sessions": 4,
            "dependencies": [{"state": "open", "incident_id": str(uuid4()), "failure_class": "availability"}],
        },
    )
    check = product_health.build_session_title_health_check(window="15m")
    assert check.verdict == "degraded"
    assert check.coverage == "full"
    assert "4 sessions blocked" in check.headline
    assert check.signals["open_availability_dependencies"] == 1
    assert check.signals["open_authentication_dependencies"] == 0
    assert check.signals["open_unclassified_dependencies"] == 0


def test_product_health_degrades_on_aged_title_backlog_with_healthy_dependency(tmp_path, monkeypatch):
    _titles_on(monkeypatch)
    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(
        catalog_read_gateway,
        "title_dependency_health",
        lambda: {
            "status": "degraded",
            "open_dependencies": 0,
            "blocked_sessions": 0,
            "pending_sessions": 3,
            "overdue_sessions": 3,
            "terminal_sessions": 0,
            "terminal_shared_failure_sessions": 0,
            "oldest_overdue_age_seconds": 600,
            "backlog_degraded_after_seconds": 300,
            "dependencies": [{"state": "healthy", "incident_id": None}],
        },
    )
    check = product_health.build_session_title_health_check(window="15m")
    assert check.verdict == "degraded"
    assert "3 obligations overdue" in check.headline
    assert check.signals["oldest_overdue_age_seconds"] == 600


def test_product_health_session_titles_is_ok_when_titles_are_off(tmp_path, monkeypatch):
    """Titles off is a normal state; the imported-session backlog it leaves is not an outage."""

    monkeypatch.setattr(product_health, "utc_now", lambda: PINNED_NOW)
    monkeypatch.setattr(storage_session_titles, "title_generation_off_reason", lambda: "transcript_egress_not_enabled")

    def _must_not_grade_the_backlog():
        raise AssertionError("graded a title backlog on a host that cannot generate titles")

    monkeypatch.setattr(catalog_read_gateway, "title_dependency_health", _must_not_grade_the_backlog)
    check = product_health.build_session_title_health_check(window="15m")
    assert check.verdict == "ok"
    assert check.coverage == "full"
    assert check.signals == {"titles_off_reason": "transcript_egress_not_enabled"}


def test_product_health_rejects_invalid_window(tmp_path):
    SessionLocal = _make_db(tmp_path)
    client = _make_client(SessionLocal)
    try:
        machine_response = client.get("/agents/observability/checks/session_titles?window=forever")
        assert machine_response.status_code == 400
        assert "Window must look like" in machine_response.json()["detail"]
    finally:
        api_app.dependency_overrides.clear()
