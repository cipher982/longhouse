from __future__ import annotations

import json
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
from zerg.models.agents import AgentsBase
from zerg.models.agents import SessionObservation


def _make_client(tmp_path):
    db_path = tmp_path / "agents_ingest_trace.db"
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

    def override_verify_agents_token():
        return SimpleNamespace(device_id="trace-device", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app), factory


def test_agents_ingest_persists_ship_trace_runtime_event(tmp_path):
    client, factory = _make_client(tmp_path)
    try:
        session_id = "d9f61d55-83e3-4d94-a2c2-f80c69a20411"
        trace = {
            "schema": "ship_trace.v1",
            "trace_id": f"{session_id}:0:128:1778220000000",
            "provider": "codex",
            "session_id": session_id,
            "work_context": "hook_catchup",
            "event_count": 1,
            "offset": 0,
            "new_offset": 128,
            "range_bytes": 128,
            "job_started_at_ms": 1778219999000,
            "prepare_started_at_ms": 1778219999100,
            "prepare_finished_at_ms": 1778219999300,
            "prepare_open_db_ms": 12,
            "prepare_binding_wait_ms": 0,
            "prepare_parse_ms": 45,
            "http_send_started_at_ms": 1778220000000,
            "prepare_ms": 200,
            "job_to_http_ms": 1000,
        }
        payload = {
            "id": session_id,
            "provider": "codex",
            "environment": "test",
            "project": "zerg",
            "started_at": "2026-01-01T00:00:00Z",
            "events": [
                {
                    "role": "assistant",
                    "content_text": "hello",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "source_path": "/tmp/trace-rollout.jsonl",
                    "source_offset": 0,
                    "raw_json": "{\"type\":\"assistant\",\"text\":\"hello\"}",
                }
            ],
        }

        response = client.post(
            "/agents/ingest",
            json=payload,
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(trace, separators=(",", ":")),
            },
        )

        assert response.status_code == 200, response.text
        with factory() as db:
            observation = (
                db.query(SessionObservation)
                .filter(SessionObservation.session_id == session_id)
                .filter(SessionObservation.source == "agents_ingest_trace")
                .one()
            )
            stored = json.loads(observation.payload_json)
            runtime_payload = stored["payload"]
            assert runtime_payload["progress_kind"] == "ship_pipeline_trace"
            assert runtime_payload["ship_trace"]["trace_id"] == trace["trace_id"]
            assert runtime_payload["ship_trace"]["prepare_ms"] == 200
            assert runtime_payload["ship_trace"]["prepare_open_db_ms"] == 12
            assert runtime_payload["ship_trace"]["prepare_parse_ms"] == 45
            assert runtime_payload["server_trace"]["store_write_ms"] >= 0
    finally:
        api_app.dependency_overrides.clear()
