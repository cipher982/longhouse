from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.models import Fiche
from zerg.models import Thread
from zerg.models import User
from zerg.models.enums import FicheStatus
from zerg.models.enums import ThreadType
from zerg.models.enums import UserRole


def _make_db(tmp_path):
    db_path = tmp_path / "test_threads_router.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
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
    api_app.dependency_overrides[get_current_user] = override_current_user

    return TestClient(app, backend="asyncio"), api_app


def test_threads_router_uses_automation_contract(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(email="owner@local", role=UserRole.USER.value)
        other = User(email="other@local", role=UserRole.USER.value)
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        automation = Fiche(
            owner_id=owner.id,
            name="Primary Automation",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        other_automation = Fiche(
            owner_id=other.id,
            name="Other Automation",
            status=FicheStatus.IDLE.value,
            system_instructions="system",
            task_instructions="task",
            model="glm-5",
        )
        db.add_all([automation, other_automation])
        db.commit()
        db.refresh(automation)
        db.refresh(other_automation)

        existing_thread = Thread(
            fiche_id=automation.id,
            title="Inbox Thread",
            fiche_state={"source": "seed"},
            thread_type=ThreadType.CHAT.value,
        )
        foreign_thread = Thread(
            fiche_id=other_automation.id,
            title="Other Thread",
            thread_type=ThreadType.CHAT.value,
        )
        db.add_all([existing_thread, foreign_thread])
        db.commit()
        db.refresh(existing_thread)

        client, api_app_ref = _make_client(db, owner)
        try:
            list_response = client.get(f"/api/threads?automation_id={automation.id}")
            assert list_response.status_code == 200, list_response.text
            list_payload = list_response.json()

            assert len(list_payload) == 1
            assert list_payload[0]["id"] == existing_thread.id
            assert list_payload[0]["automation_id"] == automation.id
            assert list_payload[0]["automation_state"] == {"source": "seed"}
            assert "fiche_id" not in list_payload[0]
            assert "fiche_state" not in list_payload[0]

            create_response = client.post(
                "/api/threads",
                json={
                    "automation_id": automation.id,
                    "title": "Created Thread",
                    "thread_type": "chat",
                    "active": True,
                    "automation_state": {"origin": "ui"},
                },
            )
            assert create_response.status_code == 201, create_response.text
            created_payload = create_response.json()

            assert created_payload["automation_id"] == automation.id
            assert created_payload["automation_state"] == {"origin": "ui"}
            assert created_payload["title"] == "Created Thread"
            assert "fiche_id" not in created_payload
            assert "fiche_state" not in created_payload
        finally:
            api_app_ref.dependency_overrides = {}
