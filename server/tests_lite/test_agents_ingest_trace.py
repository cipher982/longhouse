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
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.routers.agents_ingest import _write_serializer_label_for_ship_trace


def _make_client(tmp_path):
    db_path = tmp_path / "agents_ingest_trace.db"
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
        return SimpleNamespace(device_id="trace-device", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app), factory


def test_ship_trace_live_transcript_uses_live_ingest_label():
    assert _write_serializer_label_for_ship_trace({"work_context": "live_transcript"}) == "ingest-live"
    assert _write_serializer_label_for_ship_trace({"work_context": "reconciliation_scan"}) == "ingest-scan"
    assert _write_serializer_label_for_ship_trace({"work_context": "spool_replay"}) == "ingest-replay"
    assert _write_serializer_label_for_ship_trace(None) == "ingest"


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
                    "raw_json": '{"type":"assistant","text":"hello"}',
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
            session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
            assert session.loop_mode == "assist"

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
            fanout = (
                db.query(SessionObservation)
                .filter(SessionObservation.session_id == session_id)
                .filter(SessionObservation.source == "session_pubsub")
                .one()
            )
            stored_fanout = json.loads(fanout.payload_json)
            assert stored_fanout["ship_trace_id"] == trace["trace_id"]
            assert stored_fanout["latest_event_id"] is not None
            assert stored_fanout["server_fanout_at_ms"] is not None
    finally:
        api_app.dependency_overrides.clear()


def test_agents_ingest_emits_phase1_timing_headers(tmp_path):
    """Phase 1 instrumentation: every successful ingest response carries
    X-Ingest-Queue-Wait-Ms / X-Ingest-Exec-Ms / X-Ingest-Label so the engine
    can adapt concurrency in phase 2."""
    client, _ = _make_client(tmp_path)
    try:
        session_id = "11111111-2222-3333-4444-555555555555"
        payload = {
            "id": session_id,
            "provider": "codex",
            "environment": "test",
            "project": "zerg",
            "started_at": "2026-01-01T00:00:00Z",
            "events": [
                {
                    "role": "assistant",
                    "content_text": "hi",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "source_path": "/tmp/headers.jsonl",
                    "source_offset": 0,
                    "raw_json": '{"type":"assistant","text":"hi"}',
                }
            ],
        }
        ship_trace = {
            "schema": "ship_trace.v1",
            "trace_id": f"{session_id}:0:64:1778220000000",
            "provider": "codex",
            "session_id": session_id,
            "work_context": "spool_replay",
            "event_count": 1,
            "offset": 0,
            "new_offset": 64,
        }
        response = client.post(
            "/agents/ingest",
            json=payload,
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(ship_trace, separators=(",", ":")),
            },
        )
        assert response.status_code == 200, response.text
        assert "X-Ingest-Queue-Wait-Ms" in response.headers
        assert "X-Ingest-Exec-Ms" in response.headers
        assert response.headers.get("X-Ingest-Label") == "ingest-replay"
        float(response.headers["X-Ingest-Queue-Wait-Ms"])
        assert float(response.headers["X-Ingest-Exec-Ms"]) >= 0.0
    finally:
        api_app.dependency_overrides.clear()
