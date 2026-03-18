"""HTTP tests for the automation-first public surface."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
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
    db_path = tmp_path / "test_automations_public_surface.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(session_local, current_user):
    from zerg.main import api_app

    def override_db():
        with session_local() as db:
            yield db

    def override_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = override_user

    return TestClient(api_app), api_app


def test_automations_alias_supports_crud_and_dashboard_snapshot(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(email="owner@test.local", role=UserRole.ADMIN.value)
        db.add(owner)
        db.commit()
        db.refresh(owner)

    current_user = SimpleNamespace(id=owner.id, email=owner.email, role=owner.role)
    client, api_app = _make_client(session_local, current_user)

    try:
        create_response = client.post(
            "/automations",
            json={
                "system_instructions": "You are helpful.",
                "task_instructions": "Run the automation.",
                "model": "gpt-mock",
            },
            headers={"Idempotency-Key": "create-automation-1"},
        )
        assert create_response.status_code == 201, create_response.text
        created = create_response.json()
        automation_id = created["id"]
        assert created["display_type"] == "automation"

        list_response = client.get("/automations")
        assert list_response.status_code == 200, list_response.text
        list_payload = list_response.json()
        assert len(list_payload) == 1
        assert list_payload[0]["id"] == automation_id
        assert list_payload[0]["display_type"] == "automation"

        compatibility_response = client.get("/fiches")
        assert compatibility_response.status_code == 200, compatibility_response.text
        compatibility_payload = compatibility_response.json()
        assert [row["id"] for row in compatibility_payload] == [automation_id]

        detail_response = client.get(f"/automations/{automation_id}")
        assert detail_response.status_code == 200, detail_response.text
        assert detail_response.json()["id"] == automation_id

        details_response = client.get(f"/automations/{automation_id}/details")
        assert details_response.status_code == 200, details_response.text
        assert details_response.json()["automation"]["id"] == automation_id
        assert "fiche" not in details_response.json()

        update_response = client.put(f"/automations/{automation_id}", json={"name": "Renamed automation"})
        assert update_response.status_code == 200, update_response.text
        assert update_response.json()["name"] == "Renamed automation"

        create_message_response = client.post(
            f"/automations/{automation_id}/messages",
            json={"role": "user", "content": "hello"},
        )
        assert create_message_response.status_code == 201, create_message_response.text
        create_message_payload = create_message_response.json()
        assert create_message_payload["automation_id"] == automation_id
        assert "fiche_id" not in create_message_payload

        list_messages_response = client.get(f"/automations/{automation_id}/messages")
        assert list_messages_response.status_code == 200, list_messages_response.text
        list_messages_payload = list_messages_response.json()
        assert len(list_messages_payload) == 1
        assert list_messages_payload[0]["automation_id"] == automation_id
        assert "fiche_id" not in list_messages_payload[0]

        create_trigger_response = client.post(
            "/triggers",
            json={"automation_id": automation_id, "type": "webhook"},
        )
        assert create_trigger_response.status_code == 201, create_trigger_response.text
        create_trigger_payload = create_trigger_response.json()
        assert create_trigger_payload["automation_id"] == automation_id
        assert "fiche_id" not in create_trigger_payload

        list_triggers_response = client.get(f"/triggers?automation_id={automation_id}")
        assert list_triggers_response.status_code == 200, list_triggers_response.text
        list_triggers_payload = list_triggers_response.json()
        assert len(list_triggers_payload) == 1
        assert list_triggers_payload[0]["automation_id"] == automation_id
        assert "fiche_id" not in list_triggers_payload[0]

        overview_response = client.get("/automations/dashboard")
        assert overview_response.status_code == 200, overview_response.text
        overview_payload = overview_response.json()
        assert overview_payload["automations"][0]["id"] == automation_id
        assert overview_payload["automations"][0]["display_type"] == "automation"
        assert overview_payload["runs"] == [{"automation_id": automation_id, "runs": []}]

        openapi_response = client.get("/openapi.json")
        assert openapi_response.status_code == 200, openapi_response.text
        openapi_payload = openapi_response.json()
        schemas = openapi_payload["components"]["schemas"]
        paths = openapi_payload["paths"]
        assert "Automation" in schemas
        assert "AutomationCreate" in schemas
        assert "AutomationUpdate" in schemas
        assert "AutomationDetails" in schemas
        assert "AutomationMessage" in schemas
        assert "Fiche" not in schemas
        assert "FicheCreate" not in schemas
        assert "FicheUpdate" not in schemas
        assert "FicheDetails" not in schemas
        assert "FicheMessage" not in schemas
        assert "/automations" in paths
        assert f"/automations/{{automation_id}}" in paths
        assert f"/automations/{{automation_id}}/runs" in paths
        assert f"/automations/{{automation_id}}/connectors/" in paths
        assert f"/automations/{{automation_id}}/mcp-servers/" in paths
        assert "/fiches" not in paths
        assert f"/fiches/{{automation_id}}" not in paths
        assert f"/fiches/{{fiche_id}}/runs" not in paths
        assert f"/fiches/{{fiche_id}}/connectors/" not in paths
        assert f"/fiches/{{fiche_id}}/mcp-servers/" not in paths

        delete_response = client.delete(f"/automations/{automation_id}")
        assert delete_response.status_code == 204, delete_response.text

        final_list_response = client.get("/fiches")
        assert final_list_response.status_code == 200, final_list_response.text
        assert final_list_response.json() == []
    finally:
        api_app.dependency_overrides.clear()


def test_automations_alias_supports_task_endpoint(tmp_path, monkeypatch):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(email="owner@test.local", role=UserRole.ADMIN.value)
        db.add(owner)
        db.commit()
        db.refresh(owner)

        fiche = Fiche(
            owner_id=owner.id,
            name="Existing automation",
            status=FicheStatus.IDLE.value,
            system_instructions="You are helpful.",
            task_instructions="Run the automation.",
            model="gpt-mock",
        )
        db.add(fiche)
        db.commit()
        db.refresh(fiche)
        automation_id = fiche.id

    async def fake_execute_fiche_task(db, fiche, *, thread_type="manual", trigger="manual"):
        return SimpleNamespace(id=99)

    import zerg.services.task_runner as task_runner

    monkeypatch.setattr(task_runner, "execute_fiche_task", fake_execute_fiche_task)

    current_user = SimpleNamespace(id=owner.id, email=owner.email, role=owner.role)
    client, api_app = _make_client(session_local, current_user)

    try:
        response = client.post(f"/automations/{automation_id}/task")
        assert response.status_code == 202, response.text
        assert response.json() == {"thread_id": 99}
    finally:
        api_app.dependency_overrides.clear()


def test_automation_nested_aliases_cover_runs_connectors_and_mcp_servers(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        owner = User(email="owner@test.local", role=UserRole.ADMIN.value)
        db.add(owner)
        db.commit()
        db.refresh(owner)

        fiche = Fiche(
            owner_id=owner.id,
            name="Existing automation",
            status=FicheStatus.IDLE.value,
            system_instructions="You are helpful.",
            task_instructions="Run the automation.",
            model="gpt-mock",
        )
        db.add(fiche)
        db.commit()
        db.refresh(fiche)

        thread = Thread(
            fiche_id=fiche.id,
            title="Automation thread",
            thread_type=ThreadType.MANUAL.value,
        )
        db.add(thread)
        db.commit()
        db.refresh(thread)

        run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS.value,
            trigger=RunTrigger.MANUAL.value,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        automation_id = fiche.id

    current_user = SimpleNamespace(id=owner.id, email=owner.email, role=owner.role)
    client, api_app = _make_client(session_local, current_user)

    try:
        runs_response = client.get(f"/automations/{automation_id}/runs")
        assert runs_response.status_code == 200, runs_response.text
        runs_payload = runs_response.json()
        assert [row["id"] for row in runs_payload] == [run.id]
        assert runs_payload[0]["automation_id"] == automation_id

        connectors_response = client.get(f"/automations/{automation_id}/connectors/")
        assert connectors_response.status_code == 200, connectors_response.text
        assert len(connectors_response.json()) > 0

        mcp_response = client.get(f"/automations/{automation_id}/mcp-servers/")
        assert mcp_response.status_code == 200, mcp_response.text
        assert mcp_response.json() == []

        tools_response = client.get(f"/automations/{automation_id}/mcp-servers/available-tools")
        assert tools_response.status_code == 200, tools_response.text
        assert set(tools_response.json().keys()) == {"builtin", "mcp"}
    finally:
        api_app.dependency_overrides.clear()
