import contextlib

from fastapi.testclient import TestClient

from zerg.crud import crud
from zerg.dependencies.auth import get_current_user
from zerg.main import app


def test_ops_admin_required(client: TestClient, db_session):
    # Non-admin user
    user = crud.create_user(db_session, email="ops-user@local", provider=None, role="USER")

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        r = client.get("/api/ops/summary")
        assert r.status_code == 403
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]


def test_ops_summary_ok_for_admin(client: TestClient, db_session):
    admin = crud.create_user(db_session, email="ops-admin@local", provider=None, role="ADMIN")

    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        r = client.get("/api/ops/summary")
        assert r.status_code == 200, r.text
        data = r.json()
        assert set(
            [
                "runs_today",
                "cost_today_usd",
                "budget_user",
                "budget_global",
                "active_users_24h",
                "agents_total",
                "agents_scheduled",
                "latency_ms",
                "errors_last_hour",
                "top_agents_today",
            ]
        ).issubset(data.keys())
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
