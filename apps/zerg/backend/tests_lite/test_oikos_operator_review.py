"""Tests for proactive Oikos operator review/config endpoints."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.models.enums import UserRole
from zerg.models.user import User
from zerg.models.work import OikosWakeup
from zerg.routers.oikos_auth import get_current_oikos_user


def _make_db(tmp_path, name: str = "oikos_operator_review.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _make_client(db, current_user_id: int):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db
        finally:
            pass

    def override_current_user():
        return db.query(User).filter(User.id == current_user_id).first()

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def test_oikos_bootstrap_exposes_effective_operator_mode_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    engine, SessionLocal = _make_db(tmp_path, "bootstrap_operator_mode.db")

    with SessionLocal() as db:
        owner = User(
            email="owner@local",
            role=UserRole.USER.value,
            context={
                "display_name": "Owner",
                "preferences": {
                    "operator_mode": {
                        "enabled": True,
                        "shadow_mode": False,
                        "allow_continue": True,
                        "allow_notify": False,
                        "allow_small_repairs": True,
                    }
                },
            },
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        client, api_app_ref = _make_client(db, owner.id)
        try:
            response = client.get("/api/oikos/bootstrap")
            assert response.status_code == 200
            payload = response.json()
        finally:
            api_app_ref.dependency_overrides = {}

    engine.dispose()

    operator_mode = payload["preferences"]["operator_mode"]
    assert operator_mode == {
        "enabled": True,
        "shadow_mode": False,
        "allow_continue": True,
        "allow_notify": False,
        "allow_small_repairs": True,
    }


def test_oikos_preferences_patch_updates_operator_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    engine, SessionLocal = _make_db(tmp_path, "preferences_operator_mode.db")

    with SessionLocal() as db:
        owner = User(
            email="owner@local",
            role=UserRole.USER.value,
            context={
                "preferences": {
                    "operator_mode": {
                        "enabled": True,
                        "shadow_mode": True,
                        "allow_continue": False,
                        "allow_notify": True,
                        "allow_small_repairs": False,
                    }
                }
            },
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        client, api_app_ref = _make_client(db, owner.id)
        try:
            response = client.patch(
                "/api/oikos/preferences",
                json={
                    "operator_mode": {
                        "allow_continue": True,
                        "allow_small_repairs": True,
                        "allow_notify": False,
                    }
                },
            )
            assert response.status_code == 200
            payload = response.json()
            db.expire_all()
            refreshed = db.query(User).filter(User.id == owner.id).first()
        finally:
            api_app_ref.dependency_overrides = {}

    engine.dispose()

    assert payload["operator_mode"] == {
        "enabled": True,
        "shadow_mode": True,
        "allow_continue": True,
        "allow_notify": False,
        "allow_small_repairs": True,
    }
    assert refreshed.context["preferences"]["operator_mode"] == {
        "enabled": True,
        "shadow_mode": True,
        "allow_continue": True,
        "allow_notify": False,
        "allow_small_repairs": True,
    }


def test_oikos_wakeups_returns_owner_scoped_rows_and_supports_filters(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "oikos_wakeups.db")

    with SessionLocal() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        other = User(email="other@local", role=UserRole.USER.value)
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        db.add_all(
            [
                OikosWakeup(
                    owner_id=owner.id,
                    source="presence",
                    trigger_type="presence.blocked",
                    status="enqueued",
                    session_id="sess-owner-1",
                    run_id=101,
                    wakeup_key="presence:sess-owner-1:blocked:Bash",
                    payload={"tool_name": "Bash"},
                    created_at=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
                ),
                OikosWakeup(
                    owner_id=owner.id,
                    source="session_completed",
                    trigger_type="session_completed",
                    status="suppressed",
                    reason="stale_completion",
                    session_id="sess-owner-2",
                    wakeup_key="session_completed:sess-owner-2:task-1",
                    payload={"ended_at": "2026-03-11T11:00:00Z"},
                    created_at=datetime(2026, 3, 11, 13, 0, tzinfo=timezone.utc),
                ),
                OikosWakeup(
                    owner_id=other.id,
                    source="periodic_sweep",
                    trigger_type="periodic_sweep",
                    status="enqueued",
                    run_id=202,
                    wakeup_key="periodic_sweep:operator:sweep",
                    payload={},
                    created_at=datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()

        client, api_app_ref = _make_client(db, owner.id)
        try:
            response = client.get("/api/oikos/wakeups")
            assert response.status_code == 200
            payload = response.json()

            filtered = client.get("/api/oikos/wakeups?status=suppressed").json()
        finally:
            api_app_ref.dependency_overrides = {}

    engine.dispose()

    assert [item["id"] for item in payload] == [2, 1]
    assert [item["status"] for item in payload] == ["suppressed", "enqueued"]
    assert all(item["session_id"].startswith("sess-owner") for item in payload)
    assert filtered == [
        {
            "id": 2,
            "source": "session_completed",
            "trigger_type": "session_completed",
            "status": "suppressed",
            "reason": "stale_completion",
            "session_id": "sess-owner-2",
            "conversation_id": None,
            "wakeup_key": "session_completed:sess-owner-2:task-1",
            "run_id": None,
            "payload": {"ended_at": "2026-03-11T11:00:00Z"},
            "created_at": "2026-03-11T13:00:00Z",
        }
    ]
