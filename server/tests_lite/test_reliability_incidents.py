from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import get_db
from zerg.database import make_engine
from zerg.dependencies.auth import require_admin
from zerg.main import api_app
from zerg.database import Base
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_OPEN
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_RESOLVED
from zerg.models.work import OperationalIncident


def _make_session_local(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/test_reliability_incidents.db")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_reliability_incidents_lists_recent_open_incidents(tmp_path):
    session_local = _make_session_local(tmp_path)
    with session_local() as db:
        db.add(
            OperationalIncident(
                incident_type="stale_agent",
                source="check_stale_agents",
                dedupe_key="stale-agent:agent-1",
                status=OPERATIONAL_INCIDENT_STATUS_OPEN,
                summary="Agent 1 stale",
                context={"device_id": "agent-1"},
            )
        )
        db.add(
            OperationalIncident(
                incident_type="stale_ingest",
                source="ingest_health",
                dedupe_key="ingest-health:stale",
                status=OPERATIONAL_INCIDENT_STATUS_RESOLVED,
                summary="Ingest recovered",
                context={"resolved": True},
            )
        )
        db.commit()

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[require_admin] = lambda: object()

    try:
        client = TestClient(api_app)

        response = client.get("/reliability/incidents")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["incidents"][0]["dedupe_key"] == "stale-agent:agent-1"
        assert payload["incidents"][0]["status"] == "open"

        response = client.get("/reliability/incidents?status=all&source=ingest_health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["incidents"][0]["dedupe_key"] == "ingest-health:stale"
        assert payload["incidents"][0]["status"] == OPERATIONAL_INCIDENT_STATUS_RESOLVED
    finally:
        api_app.dependency_overrides.clear()
