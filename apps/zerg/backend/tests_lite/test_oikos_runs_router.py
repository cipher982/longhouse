"""Tests for Oikos run history endpoint."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models import Fiche
from zerg.models import Run
from zerg.models import Thread
from zerg.models import User
from zerg.models.enums import FicheStatus
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.enums import ThreadType
from zerg.models.enums import UserRole


def _make_db(tmp_path):
    """Create a SQLite DB for tests and return a session factory."""
    db_path = tmp_path / "test_oikos_runs.db"
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


def test_oikos_runs_include_task_aliases_and_support_task_filter(tmp_path):
    """GET /api/oikos/runs returns task aliases and keeps fiche filter compatibility."""
    session_local = _make_db(tmp_path)
    base_time = datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc)

    with session_local() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        other = User(email="other@local", role=UserRole.USER.value)
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        owner_primary = Fiche(
            owner_id=owner.id,
            name="Priority Inbox",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        owner_secondary = Fiche(
            owner_id=owner.id,
            name="Background Sweep",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        other_fiche = Fiche(
            owner_id=other.id,
            name="Other Owner",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        db.add_all([owner_primary, owner_secondary, other_fiche])
        db.commit()
        db.refresh(owner_primary)
        db.refresh(owner_secondary)
        db.refresh(other_fiche)

        owner_primary_thread = Thread(
            fiche_id=owner_primary.id,
            title="Priority Thread",
            thread_type=ThreadType.MANUAL.value,
        )
        owner_secondary_thread = Thread(
            fiche_id=owner_secondary.id,
            title="Background Thread",
            thread_type=ThreadType.MANUAL.value,
        )
        other_thread = Thread(
            fiche_id=other_fiche.id,
            title="Other Thread",
            thread_type=ThreadType.MANUAL.value,
        )
        db.add_all([owner_primary_thread, owner_secondary_thread, other_thread])
        db.commit()
        db.refresh(owner_primary_thread)
        db.refresh(owner_secondary_thread)
        db.refresh(other_thread)

        owner_primary_run = Run(
            fiche_id=owner_primary.id,
            thread_id=owner_primary_thread.id,
            status=RunStatus.RUNNING.value,
            trigger=RunTrigger.MANUAL.value,
            summary="Need your input",
            created_at=base_time + timedelta(minutes=2),
            updated_at=base_time + timedelta(minutes=2),
        )
        owner_secondary_run = Run(
            fiche_id=owner_secondary.id,
            thread_id=owner_secondary_thread.id,
            status=RunStatus.SUCCESS.value,
            trigger=RunTrigger.SCHEDULE.value,
            summary="Wrapped up cleanly",
            created_at=base_time + timedelta(minutes=1),
            updated_at=base_time + timedelta(minutes=1),
        )
        other_run = Run(
            fiche_id=other_fiche.id,
            thread_id=other_thread.id,
            status=RunStatus.FAILED.value,
            trigger=RunTrigger.MANUAL.value,
            error="Should not leak",
            created_at=base_time,
            updated_at=base_time,
        )
        db.add_all([owner_primary_run, owner_secondary_run, other_run])
        db.commit()
        db.refresh(owner_primary_run)
        db.refresh(owner_secondary_run)

        client, api_app_ref = _make_client(db, owner)
        try:
            response = client.get("/api/oikos/runs?limit=10")
            assert response.status_code == 200, response.text
            payload = response.json()

            assert [row["id"] for row in payload] == [owner_primary_run.id, owner_secondary_run.id]
            assert payload[0]["task_id"] == owner_primary.id
            assert payload[0]["task_name"] == "Priority Inbox"
            assert payload[0]["fiche_id"] == owner_primary.id
            assert payload[0]["fiche_name"] == "Priority Inbox"
            assert payload[0]["signal"] == "Need your input"
            assert payload[1]["task_id"] == owner_secondary.id
            assert payload[1]["task_name"] == "Background Sweep"

            task_filtered = client.get(f"/api/oikos/runs?task_id={owner_secondary.id}")
            assert task_filtered.status_code == 200, task_filtered.text
            assert [row["id"] for row in task_filtered.json()] == [owner_secondary_run.id]

            fiche_filtered = client.get(f"/api/oikos/runs?fiche_id={owner_primary.id}")
            assert fiche_filtered.status_code == 200, fiche_filtered.text
            assert [row["id"] for row in fiche_filtered.json()] == [owner_primary_run.id]
        finally:
            api_app_ref.dependency_overrides = {}
