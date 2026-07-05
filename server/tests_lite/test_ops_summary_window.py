"""Window semantics tests for retired /ops automation metrics."""

from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import require_admin
from zerg.models.models import User
from zerg.services import ops_service


def _make_db(tmp_path):
    db_path = tmp_path / "ops_summary_window.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_admin(factory):
    db = factory()

    admin = User(email="admin@local", role="ADMIN")
    db.add(admin)
    db.commit()
    db.refresh(admin)

    admin_id = admin.id
    db.close()
    return admin_id


def _client(factory, admin_id: int):
    from zerg.main import api_app

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_require_admin():
        return SimpleNamespace(id=admin_id)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[require_admin] = override_require_admin
    return TestClient(api_app), api_app


def test_ops_summary_window_scopes_runs_cost_and_labels(tmp_path, monkeypatch):
    fixed_today = date(2026, 3, 3)

    monkeypatch.setattr(ops_service, "_today_date_utc", lambda: fixed_today)

    factory = _make_db(tmp_path)
    admin_id = _seed_admin(factory)
    client, api_app_ref = _client(factory, admin_id)

    try:
        today_resp = client.get("/ops/summary?window=today")
        seven_resp = client.get("/ops/summary?window=7d")
        thirty_resp = client.get("/ops/summary?window=30d")

        assert today_resp.status_code == 200
        assert seven_resp.status_code == 200
        assert thirty_resp.status_code == 200

        today = today_resp.json()
        seven = seven_resp.json()
        thirty = thirty_resp.json()

        # Canonical window fields
        assert today["window"] == "today"
        assert seven["window"] == "7d"
        assert thirty["window"] == "30d"
        assert today["window_label"] == "Today"
        assert seven["window_label"] == "Last 7 Days"
        assert thirty["window_label"] == "Last 30 Days"

        # The pre-launch automation data plane is retired; keep the surface
        # stable while reporting empty launch-era metrics.
        for payload in (today, seven, thirty):
            assert payload["runs"] == 0
            assert payload["cost_usd"] is None
            assert payload["errors_last_hour"] == 0
            assert payload["top_automations"] == []

    finally:
        api_app_ref.dependency_overrides.clear()
