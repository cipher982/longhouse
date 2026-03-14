"""Tests for Oikos fiche listing endpoint."""

from datetime import datetime

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models import Fiche
from zerg.models import User
from zerg.models.enums import FicheStatus
from zerg.models.enums import UserRole


def _make_db(tmp_path):
    """Create a SQLite DB for tests and return a session factory."""
    db_path = tmp_path / "test_oikos_fiches.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    """Create TestClient with DB + Oikos auth dependency overrides."""
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user

    return TestClient(app, backend="asyncio"), api_app


def test_oikos_fiches_returns_next_run_at_and_scopes_to_owner(tmp_path):
    """GET /api/oikos/fiches returns stored next_run_at and filters by owner."""
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        other = User(email="other@local", role=UserRole.USER.value)
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        owner_next_run = datetime(2026, 3, 5, 14, 30, 0)
        db.add_all(
            [
                Fiche(
                    owner_id=owner.id,
                    name="Owner Fiche",
                    status=FicheStatus.IDLE.value,
                    system_instructions="owner system instructions",
                    task_instructions="owner task instructions",
                    model="glm-5",
                    schedule="30 14 * * *",
                    next_run_at=owner_next_run,
                ),
                Fiche(
                    owner_id=other.id,
                    name="Other Fiche",
                    status=FicheStatus.IDLE.value,
                    system_instructions="other system instructions",
                    task_instructions="other task instructions",
                    model="glm-5",
                    schedule="0 9 * * *",
                    next_run_at=datetime(2026, 3, 6, 9, 0, 0),
                ),
            ]
        )
        db.commit()

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.get("/api/oikos/fiches")
            assert response.status_code == 200
            payload = response.json()

            assert len(payload) == 1
            assert payload[0]["name"] == "Owner Fiche"
            assert payload[0]["schedule"] == "30 14 * * *"
            assert payload[0]["next_run_at"] is not None
            assert payload[0]["next_run_at"].startswith("2026-03-05T14:30:00")
            assert payload[0]["next_run_at"].endswith("Z")
        finally:
            api_app_ref.dependency_overrides = {}
