"""Events API should expose active-context membership without hiding forensic history."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import api_app
from zerg.models.agents import AgentsBase


def _make_client(tmp_path):
    db_path = tmp_path / "active_context_flag.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override
    return TestClient(api_app)


def _ingest_compacted_session(client: TestClient, session_id: str) -> None:
    payload = {
        "id": session_id,
        "provider": "claude",
        "environment": "production",
        "started_at": "2026-01-01T00:00:00Z",
        "events": [
            {
                "role": "user",
                "content_text": "My favorite color is yellow.",
                "timestamp": "2026-01-01T00:00:01Z",
                "source_path": "/tmp/claude.jsonl",
                "source_offset": 10,
                "raw_json": '{"type":"user","timestamp":"2026-01-01T00:00:01Z"}',
            },
            {
                "role": "assistant",
                "content_text": "Noted.",
                "timestamp": "2026-01-01T00:00:02Z",
                "source_path": "/tmp/claude.jsonl",
                "source_offset": 20,
                "raw_json": '{"type":"assistant","timestamp":"2026-01-01T00:00:02Z"}',
            },
            {
                "role": "system",
                "content_text": "Conversation compacted [trigger=auto]",
                "timestamp": "2026-01-01T00:00:03Z",
                "source_path": "/tmp/claude.jsonl",
                "source_offset": 30,
                "raw_json": '{"type":"system","subtype":"compact_boundary","timestamp":"2026-01-01T00:00:03Z"}',
            },
            {
                "role": "assistant",
                "content_text": "Continuing with the current context only.",
                "timestamp": "2026-01-01T00:00:04Z",
                "source_path": "/tmp/claude.jsonl",
                "source_offset": 40,
                "raw_json": '{"type":"assistant","timestamp":"2026-01-01T00:00:04Z"}',
            },
        ],
    }
    response = client.post(
        "/agents/ingest",
        json=payload,
        headers={"X-Agents-Token": "dev"},
    )
    assert response.status_code == 200, response.text


def test_events_api_marks_forensic_rows_outside_active_context(tmp_path):
    client = _make_client(tmp_path)
    try:
        session_id = "aaaaaaaa-0000-0000-0000-000000000099"
        _ingest_compacted_session(client, session_id)

        forensic_resp = client.get(
            f"/agents/sessions/{session_id}/events",
            headers={"X-Agents-Token": "dev"},
        )
        assert forensic_resp.status_code == 200, forensic_resp.text
        forensic_events = forensic_resp.json()["events"]
        assert [event["in_active_context"] for event in forensic_events] == [False, False, True, True]

        active_resp = client.get(
            f"/agents/sessions/{session_id}/events",
            params={"context_mode": "active_context"},
            headers={"X-Agents-Token": "dev"},
        )
        assert active_resp.status_code == 200, active_resp.text
        active_events = active_resp.json()["events"]
        assert len(active_events) == 2
        assert all(event["in_active_context"] is True for event in active_events)
    finally:
        api_app.dependency_overrides.clear()
