"""Events API branch-mode projection for rewind branches."""

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app


def _make_client(tmp_path):
    db_path = tmp_path / "events_branch_mode.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="branch-mode", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app)


def _ingest(client: TestClient, payload: dict) -> None:
    response = client.post("/agents/ingest", json=payload, headers={"X-Agents-Token": "dev"})
    assert response.status_code == 200, response.text


def test_events_api_branch_mode_head_vs_all(tmp_path):
    client = _make_client(tmp_path)
    try:
        session_id = "aaaaaaaa-0000-0000-0000-000000000155"
        source_path = "/tmp/rewind-api.jsonl"

        _ingest(
            client,
            {
                "id": session_id,
                "provider": "claude",
                "environment": "production",
                "started_at": "2026-01-01T00:00:00Z",
                "events": [
                    {
                        "role": "user",
                        "content_text": "start",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "source_path": source_path,
                        "source_offset": 0,
                        "raw_json": '{"type":"user","text":"start"}',
                    },
                    {
                        "role": "assistant",
                        "content_text": "old middle",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "source_path": source_path,
                        "source_offset": 10,
                        "raw_json": '{"type":"assistant","text":"old middle"}',
                    },
                    {
                        "role": "assistant",
                        "content_text": "old tail",
                        "timestamp": "2026-01-01T00:00:03Z",
                        "source_path": source_path,
                        "source_offset": 20,
                        "raw_json": '{"type":"assistant","text":"old tail"}',
                    },
                ],
                "source_lines": [
                    {"source_path": source_path, "source_offset": 0, "raw_json": '{"type":"user","text":"start"}'},
                    {"source_path": source_path, "source_offset": 10, "raw_json": '{"type":"assistant","text":"old middle"}'},
                    {"source_path": source_path, "source_offset": 20, "raw_json": '{"type":"assistant","text":"old tail"}'},
                ],
            },
        )

        _ingest(
            client,
            {
                "id": session_id,
                "provider": "claude",
                "environment": "production",
                "started_at": "2026-01-01T00:00:00Z",
                "events": [
                    {
                        "role": "assistant",
                        "content_text": "rewritten middle",
                        "timestamp": "2026-01-01T00:00:04Z",
                        "source_path": source_path,
                        "source_offset": 10,
                        "raw_json": '{"type":"assistant","text":"rewritten middle"}',
                    },
                    {
                        "role": "assistant",
                        "content_text": "new tail",
                        "timestamp": "2026-01-01T00:00:05Z",
                        "source_path": source_path,
                        "source_offset": 30,
                        "raw_json": '{"type":"assistant","text":"new tail"}',
                    },
                ],
                "source_lines": [
                    {"source_path": source_path, "source_offset": 10, "raw_json": '{"type":"assistant","text":"rewritten middle"}'},
                    {"source_path": source_path, "source_offset": 30, "raw_json": '{"type":"assistant","text":"new tail"}'},
                ],
            },
        )

        head_resp = client.get(f"/agents/sessions/{session_id}/events", headers={"X-Agents-Token": "dev"})
        assert head_resp.status_code == 200, head_resp.text
        head_data = head_resp.json()
        assert head_data["branch_mode"] == "head"
        assert head_data["total"] == 3
        assert head_data["abandoned_events"] >= 1
        assert [row["content_text"] for row in head_data["events"] if row["content_text"]] == [
            "start",
            "rewritten middle",
            "new tail",
        ]
        assert all(row["is_head_branch"] is True for row in head_data["events"])

        all_resp = client.get(
            f"/agents/sessions/{session_id}/events",
            params={"branch_mode": "all"},
            headers={"X-Agents-Token": "dev"},
        )
        assert all_resp.status_code == 200, all_resp.text
        all_data = all_resp.json()
        assert all_data["branch_mode"] == "all"
        assert all_data["abandoned_events"] == 0
        assert all_data["total"] > head_data["total"]
        assert any(row["content_text"] == "old tail" for row in all_data["events"])
        assert any(row["is_head_branch"] is False for row in all_data["events"])

        bad_resp = client.get(
            f"/agents/sessions/{session_id}/events",
            params={"branch_mode": "bad"},
            headers={"X-Agents-Token": "dev"},
        )
        assert bad_resp.status_code == 400
        assert "branch_mode" in bad_resp.json()["detail"]
    finally:
        api_app.dependency_overrides.clear()


def test_events_api_anchor_tail_returns_latest_window(tmp_path):
    client = _make_client(tmp_path)
    try:
        session_id = "aaaaaaaa-0000-0000-0000-000000000177"
        source_path = "/tmp/tail-api.jsonl"
        events = []
        source_lines = []
        for idx in range(1, 6):
            raw_json = f'{{"type":"user","text":"event {idx}"}}'
            events.append(
                {
                    "role": "user",
                    "content_text": f"event {idx}",
                    "timestamp": f"2026-01-01T00:00:0{idx}Z",
                    "source_path": source_path,
                    "source_offset": idx,
                    "raw_json": raw_json,
                }
            )
            source_lines.append(
                {
                    "source_path": source_path,
                    "source_offset": idx,
                    "raw_json": raw_json,
                }
            )

        _ingest(
            client,
            {
                "id": session_id,
                "provider": "claude",
                "environment": "production",
                "started_at": "2026-01-01T00:00:00Z",
                "events": events,
                "source_lines": source_lines,
            },
        )

        response = client.get(
            f"/agents/sessions/{session_id}/events",
            params={"limit": 2, "anchor": "tail"},
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 5
        assert [row["content_text"] for row in data["events"]] == ["event 4", "event 5"]

        previous_response = client.get(
            f"/agents/sessions/{session_id}/events",
            params={"limit": 2, "anchor": "tail", "offset": 2},
            headers={"X-Agents-Token": "dev"},
        )
        assert previous_response.status_code == 200, previous_response.text
        previous_data = previous_response.json()
        assert [row["content_text"] for row in previous_data["events"]] == ["event 2", "event 3"]

        bad_response = client.get(
            f"/agents/sessions/{session_id}/events",
            params={"anchor": "middle"},
            headers={"X-Agents-Token": "dev"},
        )
        assert bad_response.status_code == 400
        assert "anchor" in bad_response.json()["detail"]
    finally:
        api_app.dependency_overrides.clear()
