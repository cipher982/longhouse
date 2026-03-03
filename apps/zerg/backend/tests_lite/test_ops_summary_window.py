"""Window semantics tests for /ops/summary."""

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import require_admin
from zerg.models.models import Fiche
from zerg.models.models import Run
from zerg.models.models import Thread
from zerg.models.models import User
from zerg.services import ops_service


def _make_db(tmp_path):
    db_path = tmp_path / "ops_summary_window.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_runs(factory, now_utc: datetime):
    db = factory()

    admin = User(email="admin@local", role="ADMIN")
    db.add(admin)
    db.commit()
    db.refresh(admin)

    fiche = Fiche(
        name="Ops Test Fiche",
        system_instructions="system",
        task_instructions="task",
        model="glm-5",
        owner_id=admin.id,
    )
    db.add(fiche)
    db.commit()
    db.refresh(fiche)

    thread = Thread(fiche_id=fiche.id, title="Ops Thread")
    db.add(thread)
    db.commit()
    db.refresh(thread)

    def add_run(*, days_ago: int, status: str, cost_usd: float | None, duration_ms: int | None, finished_offset_mins: int = 2):
        started = now_utc - timedelta(days=days_ago)
        finished = started + timedelta(minutes=finished_offset_mins)
        run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=status,
            trigger="manual",
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            total_cost_usd=cost_usd,
        )
        db.add(run)

    # Within today window
    add_run(days_ago=0, status="success", cost_usd=1.0, duration_ms=100)

    # Within 7d window only
    add_run(days_ago=2, status="success", cost_usd=2.0, duration_ms=200)

    # Within 30d window only
    add_run(days_ago=20, status="success", cost_usd=3.0, duration_ms=300)

    # Failed run in the last hour (should affect errors_last_hour and run counts)
    failed_started = now_utc - timedelta(minutes=50)
    failed = Run(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status="failed",
        trigger="manual",
        started_at=failed_started,
        finished_at=failed_started + timedelta(minutes=1),
        duration_ms=None,
        total_cost_usd=None,
    )
    db.add(failed)

    db.commit()
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
    fixed_now = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    fixed_today = date(2026, 3, 3)

    monkeypatch.setattr(ops_service, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(ops_service, "_today_date_utc", lambda: fixed_today)

    factory = _make_db(tmp_path)
    admin_id = _seed_runs(factory, fixed_now)
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

        # Window-scoped metrics
        assert today["runs"] == 2
        assert seven["runs"] == 3
        assert thirty["runs"] == 4

        assert today["cost_usd"] == pytest.approx(1.0)
        assert seven["cost_usd"] == pytest.approx(3.0)
        assert thirty["cost_usd"] == pytest.approx(6.0)

        # Fixed realtime metrics
        assert today["errors_last_hour"] == 1
        assert seven["errors_last_hour"] == 1
        assert thirty["errors_last_hour"] == 1

        # Top fiches should reflect selected window totals
        assert today["top_fiches"][0]["runs"] == 2
        assert seven["top_fiches"][0]["runs"] == 3
        assert thirty["top_fiches"][0]["runs"] == 4

    finally:
        api_app_ref.dependency_overrides.clear()
