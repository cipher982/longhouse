from __future__ import annotations

import json
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import pytest
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.routers.agents_ingest import _ARCHIVE_INGEST_MAX_IN_FLIGHT
from zerg.routers.agents_ingest import _acquire_archive_ingest_slot
from zerg.routers.agents_ingest import _ingest_lane_for_label
from zerg.routers.agents_ingest import _release_archive_ingest_slot
from zerg.routers.agents_ingest import _stage_timing_header_value
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
    assert _ingest_lane_for_label("ingest-live") == "live"
    assert _ingest_lane_for_label("ingest-replay") == "archive"
    assert _ingest_lane_for_label("ingest-scan") == "archive"
    assert _ingest_lane_for_label("ingest") == "default"


def test_stage_timing_header_value_is_bounded_and_sorted():
    value = _stage_timing_header_value(
        {
            "total": 123.456,
            "provider_event_observations": 45.55,
            "source_line_observations": 30.1,
            "commit_after_turns": 1.2,
            "bad": -1,
        }
    )
    assert json.loads(value) == {
        "commit_after_turns": 1.2,
        "provider_event_observations": 45.5,
        "source_line_observations": 30.1,
        "total": 123.5,
    }


@pytest.mark.asyncio
async def test_archive_ingest_admission_rejects_when_archive_slot_busy():
    acquired_slots = []
    try:
        for _ in range(_ARCHIVE_INGEST_MAX_IN_FLIGHT):
            acquired = await _acquire_archive_ingest_slot("ingest-replay", Response())
            assert acquired is True
            acquired_slots.append(acquired)

        response = Response()
        with pytest.raises(HTTPException) as exc:
            await _acquire_archive_ingest_slot("ingest-scan", response)
        assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "Archive ingest backlog is throttled" in exc.value.detail
        assert response.headers["Retry-After"] == "5"
        assert response.headers["X-Ingest-Lane"] == "archive"
        assert response.headers["X-Ingest-Admission-State"] == "archive_slots_full"
        assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
        assert response.headers["X-Ingest-Error-Kind"] == "archive_ingest_backpressure"
        assert response.headers["X-Ingest-Queue-Wait-Ms"] == "0.0"
        assert response.headers["X-Ingest-Exec-Ms"] == "0.0"
    finally:
        for acquired in acquired_slots:
            _release_archive_ingest_slot(acquired)


@pytest.mark.asyncio
async def test_archive_ingest_admission_rejects_when_writer_is_busy(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-replay"
        active_age_ms = 50.0
        queue_depth = 0

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _acquire_archive_ingest_slot("ingest-replay", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "5"
    assert response.headers["X-Ingest-Admission-State"] == "archive_writer_active"
    assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
    assert response.headers["X-Ingest-Writer-Active-Label"] == "ingest-replay"


@pytest.mark.asyncio
async def test_archive_ingest_admission_rejects_when_writer_queue_is_busy(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 2

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _acquire_archive_ingest_slot("ingest-replay", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "5"
    assert response.headers["X-Ingest-Admission-State"] == "writer_queue_pressure"
    assert response.headers["X-Ingest-Writer-Queue-Depth"] == "2"


@pytest.mark.asyncio
async def test_archive_ingest_admission_allows_short_non_archive_writer(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "heartbeat"
        active_age_ms = 50.0
        queue_depth = 0

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    acquired = await _acquire_archive_ingest_slot("ingest-replay", response)
    try:
        assert acquired is True
        assert "Retry-After" not in response.headers
    finally:
        _release_archive_ingest_slot(acquired)


@pytest.mark.asyncio
async def test_live_ingest_admission_ignores_archive_writer_pressure(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-replay"
        active_age_ms = 10_000.0
        queue_depth = 10

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    acquired = await _acquire_archive_ingest_slot("ingest-live", response)
    assert acquired is False
    assert "Retry-After" not in response.headers


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
    X-Ingest-Queue-Wait-Ms / X-Ingest-Exec-Ms / X-Ingest-Label plus lane and
    admission state so the engine can adapt concurrency in phase 2."""
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
        assert response.headers.get("X-Ingest-Lane") == "archive"
        assert response.headers.get("X-Ingest-Admission-State") == "archive_slot_acquired"
        stage_ms = json.loads(response.headers["X-Ingest-Store-Stage-Ms"])
        assert stage_ms["total"] >= 0.0
        assert "provider_event_observations" in stage_ms
        float(response.headers["X-Ingest-Queue-Wait-Ms"])
        assert float(response.headers["X-Ingest-Exec-Ms"]) >= 0.0
    finally:
        api_app.dependency_overrides.clear()
