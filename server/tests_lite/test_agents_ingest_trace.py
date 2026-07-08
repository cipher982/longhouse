from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-long-enough")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-long-enough")
os.environ.setdefault("AUTH_DISABLED", "1")

import pytest
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionObservation
from zerg.routers.agents_ingest import _ARCHIVE_INGEST_MAX_IN_FLIGHT
from zerg.routers.agents_ingest import _acquire_archive_ingest_slot
from zerg.routers.agents_ingest import _archive_retry_after_for_queue_depth
from zerg.routers.agents_ingest import _check_live_ingest_writer_pressure
from zerg.routers.agents_ingest import _incremental_session_counts_for_label
from zerg.routers.agents_ingest import _ingest_lane_for_label
from zerg.routers.agents_ingest import _ingest_queue_timeout_for_label
from zerg.routers.agents_ingest import _ingest_write_timeout_for_label
from zerg.routers.agents_ingest import _release_archive_ingest_slot
from zerg.routers.agents_ingest import _stage_timing_header_value
from zerg.routers.agents_ingest import _sync_derived_projections_for_label
from zerg.routers.agents_ingest import _sync_session_counts_for_label
from zerg.routers.agents_ingest import _write_serializer_label_for_ship_trace
from zerg.services.write_serializer import InterruptedWriteError
from zerg.services.write_serializer import WriteQueueTimeoutError


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
    assert _write_serializer_label_for_ship_trace({"work_context": "hook_catchup"}) == "ingest"
    assert _write_serializer_label_for_ship_trace(None) == "ingest"
    assert _ingest_lane_for_label("ingest-live") == "live"
    assert _ingest_lane_for_label("ingest-replay") == "archive"
    assert _ingest_lane_for_label("ingest-scan") == "archive"
    assert _ingest_lane_for_label("ingest") == "archive"
    assert not _sync_session_counts_for_label("ingest")
    assert _incremental_session_counts_for_label("ingest")
    assert not _sync_derived_projections_for_label("ingest")
    assert not _sync_session_counts_for_label("ingest-replay")
    assert not _incremental_session_counts_for_label("ingest-replay")
    assert not _sync_derived_projections_for_label("ingest-replay")


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


def test_archive_retry_after_scales_with_queue_depth():
    assert _archive_retry_after_for_queue_depth(1) == 5
    assert _archive_retry_after_for_queue_depth(23) == 46
    assert _archive_retry_after_for_queue_depth(999) == 60


def test_archive_backpressure_headers_survive_http_exception(tmp_path, monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 50

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    client, _ = _make_client(tmp_path)
    try:
        response = client.post(
            "/agents/ingest",
            json={},
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(
                    {
                        "schema": "ship_trace.v1",
                        "work_context": "spool_replay",
                    },
                    separators=(",", ":"),
                ),
            },
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.headers["Retry-After"] == "60"
        assert response.headers["X-Ingest-Lane"] == "archive"
        assert response.headers["X-Ingest-Admission-State"] == "writer_queue_pressure"
        assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
        assert response.headers["X-Ingest-Error-Kind"] == "archive_ingest_backpressure"
        assert response.headers["X-Ingest-Queue-Wait-Ms"] == "0.0"
        assert response.headers["X-Ingest-Exec-Ms"] == "0.0"
    finally:
        api_app.dependency_overrides.clear()


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
async def test_archive_ingest_admission_rejects_active_archive_writer(monkeypatch):
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
    assert response.headers["Retry-After"] == "15"
    assert response.headers["X-Ingest-Admission-State"] == "archive_writer_busy"
    assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
    assert response.headers["X-Ingest-Writer-Active-Label"] == "ingest-replay"
    assert response.headers["X-Ingest-Writer-Active-Age-Ms"] == "50.0"


@pytest.mark.asyncio
async def test_archive_ingest_admission_rejects_stale_active_archive_writer(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-replay"
        active_age_ms = 5000.0
        queue_depth = 0

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _acquire_archive_ingest_slot("ingest-replay", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "15"
    assert response.headers["X-Ingest-Admission-State"] == "archive_writer_busy"
    assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
    assert response.headers["X-Ingest-Writer-Active-Label"] == "ingest-replay"
    assert response.headers["X-Ingest-Writer-Active-Age-Ms"] == "5000.0"


@pytest.mark.asyncio
async def test_archive_ingest_admission_surfaces_writer_queue_without_rejecting(monkeypatch):
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

    acquired = await _acquire_archive_ingest_slot("ingest-replay", response)
    try:
        assert acquired is True
        assert response.headers["X-Ingest-Writer-Queue-Depth"] == "2"
        assert "Retry-After" not in response.headers
    finally:
        _release_archive_ingest_slot(acquired)


@pytest.mark.asyncio
async def test_archive_ingest_admission_rejects_when_writer_queue_hits_hard_limit(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 50

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _acquire_archive_ingest_slot("ingest-replay", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "60"
    assert response.headers["X-Ingest-Admission-State"] == "writer_queue_pressure"
    assert response.headers["X-Ingest-Writer-Queue-Depth"] == "50"


@pytest.mark.asyncio
async def test_archive_ingest_admission_rejects_when_archive_wal_pressure_sheds(monkeypatch):
    class QuietSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 0

    monkeypatch.setenv("LONGHOUSE_ARCHIVE_INGEST_WAL_SHED_BYTES", "100")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_INGEST_WAL_RETRY_AFTER_SECONDS", "17")
    monkeypatch.setattr("zerg.database.get_wal_bytes", lambda: 100)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: QuietSerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _acquire_archive_ingest_slot("ingest-replay", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "17"
    assert response.headers["X-Ingest-Lane"] == "archive"
    assert response.headers["X-Ingest-Admission-State"] == "archive_wal_pressure"
    assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
    assert response.headers["X-Ingest-Archive-Wal-Bytes"] == "100"
    assert response.headers["X-Ingest-Archive-Wal-Shed-Threshold-Bytes"] == "100"


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
async def test_archive_ingest_admission_allows_non_archive_writer_to_finish(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-live"
        active_age_ms = 5000.0
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
        assert "X-Ingest-Admission-State" not in response.headers
        assert "X-Ingest-Backpressure" not in response.headers
        assert "X-Ingest-Writer-Active-Label" not in response.headers
    finally:
        _release_archive_ingest_slot(acquired)


@pytest.mark.asyncio
async def test_live_ingest_admission_rejects_stale_archive_writer(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-replay"
        active_age_ms = 10_000.0
        queue_depth = 9

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    acquired = await _acquire_archive_ingest_slot("ingest-live", response)
    assert acquired is False
    with pytest.raises(HTTPException) as exc:
        await _check_live_ingest_writer_pressure("ingest-live", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "5"
    assert response.headers["X-Ingest-Lane"] == "live"
    assert response.headers["X-Ingest-Admission-State"] == "writer_pressure"
    assert response.headers["X-Ingest-Backpressure"] == "live_ingest_backpressure"
    assert response.headers["X-Ingest-Writer-Active-Label"] == "ingest-replay"
    assert response.headers["X-Ingest-Writer-Active-Age-Ms"] == "10000.0"


@pytest.mark.asyncio
async def test_live_ingest_admission_rejects_stale_live_writer(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-live"
        active_age_ms = 10_000.0
        queue_depth = 2

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _check_live_ingest_writer_pressure("ingest-live", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "5"
    assert response.headers["X-Ingest-Lane"] == "live"
    assert response.headers["X-Ingest-Admission-State"] == "live_writer_busy"
    assert response.headers["X-Ingest-Backpressure"] == "live_ingest_backpressure"
    assert response.headers["X-Ingest-Writer-Queue-Depth"] == "2"
    assert response.headers["X-Ingest-Writer-Active-Label"] == "ingest-live"


@pytest.mark.asyncio
async def test_live_ingest_admission_rejects_large_writer_queue(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 10

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _check_live_ingest_writer_pressure("ingest-live", response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["Retry-After"] == "20"
    assert response.headers["X-Ingest-Lane"] == "live"
    assert response.headers["X-Ingest-Admission-State"] == "writer_queue_pressure"
    assert response.headers["X-Ingest-Backpressure"] == "live_ingest_backpressure"
    assert response.headers["X-Ingest-Writer-Queue-Depth"] == "10"


@pytest.mark.asyncio
async def test_untraced_ingest_uses_archive_admission(monkeypatch):
    class BusySerializer:
        is_configured = True
        writer_active = True
        active_label = "ingest-replay"
        active_age_ms = 5000.0
        queue_depth = 0

    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: BusySerializer(),
    )
    response = Response()

    with pytest.raises(HTTPException) as exc:
        await _acquire_archive_ingest_slot(_write_serializer_label_for_ship_trace(None), response)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["X-Ingest-Lane"] == "archive"
    assert response.headers["X-Ingest-Admission-State"] == "archive_writer_busy"
    assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"


def test_large_untraced_ingest_backpressures_before_writer(tmp_path):
    client, _ = _make_client(tmp_path)
    try:
        payload = {
            "id": "d9f61d55-83e3-4d94-a2c2-f80c69a20411",
            "provider": "codex",
            "environment": "test",
            "project": "zerg",
            "started_at": "2026-01-01T00:00:00Z",
            "events": [
                {
                    "role": "assistant",
                    "content_text": f"hello {idx}",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "source_path": "/tmp/untraced-large.jsonl",
                    "source_offset": idx,
                    "raw_json": json.dumps({"type": "assistant", "text": f"hello {idx}"}),
                }
                for idx in range(201)
            ],
        }

        response = client.post(
            "/agents/ingest",
            json=payload,
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.headers["X-Ingest-Lane"] == "archive"
        assert response.headers["X-Ingest-Admission-State"] == "untraced_ingest_too_large"
        assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
    finally:
        api_app.dependency_overrides.clear()


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


def test_archive_ingest_passes_bounded_serializer_timeout(tmp_path, monkeypatch):
    calls: list[dict] = []

    class RecordingSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 0

        async def execute_after_closing_request_session(self, fn, fallback_db, **kwargs):
            calls.append(kwargs)
            assert kwargs["label"] == "ingest-replay"
            assert kwargs["timeout_seconds"] == _ingest_write_timeout_for_label("ingest-replay")
            assert kwargs["queue_timeout_seconds"] == _ingest_queue_timeout_for_label("ingest-replay")
            return fn(fallback_db)

    client, _ = _make_client(tmp_path)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: RecordingSerializer(),
    )
    try:
        session_id = "21111111-2222-3333-4444-555555555555"
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
                    "source_path": "/tmp/write-timeout.jsonl",
                    "source_offset": 0,
                    "raw_json": '{"type":"assistant","text":"hi"}',
                }
            ],
        }
        response = client.post(
            "/agents/ingest",
            json=payload,
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(
                    {
                        "schema": "ship_trace.v1",
                        "trace_id": f"{session_id}:0:64:1778220000000",
                        "provider": "codex",
                        "session_id": session_id,
                        "work_context": "spool_replay",
                    },
                    separators=(",", ":"),
                ),
            },
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert calls
        assert response.headers["X-Ingest-Lane"] == "archive"
        assert response.headers["X-Ingest-Admission-State"] == "archive_slot_acquired"
        assert response.headers["X-Ingest-Sub-Batches"] == "1"
    finally:
        api_app.dependency_overrides.clear()


def test_archive_ingest_queue_timeout_returns_retryable_backpressure(tmp_path, monkeypatch):
    class QueueTimingOutSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 8

        async def execute_after_closing_request_session(self, _fn, _fallback_db, **kwargs):
            raise WriteQueueTimeoutError(
                label=kwargs["label"],
                queue_timeout_seconds=kwargs["queue_timeout_seconds"],
            )

    client, _ = _make_client(tmp_path)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: QueueTimingOutSerializer(),
    )
    try:
        session_id = "24111111-2222-3333-4444-555555555555"
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
                    "source_path": "/tmp/queue-timeout-ingest.jsonl",
                    "source_offset": 0,
                    "raw_json": '{"type":"assistant","text":"hi"}',
                }
            ],
        }
        response = client.post(
            "/agents/ingest",
            json=payload,
            headers=_spool_replay_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.headers["Retry-After"] == "5"
        assert response.headers["X-Ingest-Lane"] == "archive"
        assert response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"
        assert response.headers["X-Ingest-Error-Kind"] == "archive_ingest_backpressure"
        assert response.headers["X-Ingest-Admission-State"] == "writer_queue_timeout"
        assert response.headers["X-Ingest-Queue-Timeout-Label"] == "ingest-replay"
        assert response.headers["X-Ingest-Queue-Timeout-Seconds"] == "6.0"
    finally:
        api_app.dependency_overrides.clear()


def test_archive_ingest_request_budget_exhaustion_returns_retryable_backpressure(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_INGEST_REQUEST_BUDGET_SECONDS", "0.05")

    class SlowSuccessfulSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 0

        async def execute_after_closing_request_session(self, fn, fallback_db, **_kwargs):
            result = fn(fallback_db)
            await asyncio.sleep(0.08)
            return result

    client, SessionLocal = _make_client(tmp_path)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: SlowSuccessfulSerializer(),
    )
    try:
        session_id = "24511111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 17),
            headers=_spool_replay_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.headers["Retry-After"] == "5"
        assert response.headers["X-Ingest-Lane"] == "archive"
        assert response.headers["X-Ingest-Admission-State"] == "request_budget_exhausted"
        assert response.headers["X-Ingest-Completed-Sub-Batches"] == "1"
        assert response.headers["X-Ingest-Request-Budget-Seconds"] == "0.1"
        assert float(response.headers["X-Ingest-Request-Elapsed-Seconds"]) >= 0.05
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 16
    finally:
        api_app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("work_context", "expected_lane", "expected_kind", "expected_retry_after"),
    [
        ("live_transcript", "live", "live_ingest_backpressure", "5"),
        ("spool_replay", "archive", "archive_ingest_backpressure", "15"),
    ],
)
def test_timed_out_ingest_write_returns_retryable_backpressure(
    tmp_path,
    monkeypatch,
    work_context,
    expected_lane,
    expected_kind,
    expected_retry_after,
):
    class TimingOutSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 0

        async def execute_after_closing_request_session(self, _fn, _fallback_db, **_kwargs):
            raise asyncio.TimeoutError()

    client, _ = _make_client(tmp_path)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: TimingOutSerializer(),
    )
    try:
        session_id = "25111111-2222-3333-4444-555555555555"
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
                    "source_path": "/tmp/timed-out-ingest.jsonl",
                    "source_offset": 0,
                    "raw_json": '{"type":"assistant","text":"hi"}',
                }
            ],
        }
        response = client.post(
            "/agents/ingest",
            json=payload,
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(
                    {
                        "schema": "ship_trace.v1",
                        "trace_id": f"{session_id}:0:64:1778220000000",
                        "provider": "codex",
                        "session_id": session_id,
                        "work_context": work_context,
                    },
                    separators=(",", ":"),
                ),
            },
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.headers["Retry-After"] == expected_retry_after
        assert response.headers["X-Ingest-Lane"] == expected_lane
        assert response.headers["X-Ingest-Backpressure"] == expected_kind
        assert response.headers["X-Ingest-Error-Kind"] == expected_kind
        assert response.headers["X-Ingest-Admission-State"] == "writer_timeout"
    finally:
        api_app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("work_context", "expected_lane", "expected_kind", "expected_retry_after"),
    [
        ("live_transcript", "live", "live_ingest_backpressure", "5"),
        ("spool_replay", "archive", "archive_ingest_backpressure", "15"),
    ],
)
def test_interrupted_ingest_write_returns_retryable_backpressure(
    tmp_path,
    monkeypatch,
    work_context,
    expected_lane,
    expected_kind,
    expected_retry_after,
):
    class InterruptingSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 0

        async def execute_after_closing_request_session(self, _fn, _fallback_db, **kwargs):
            raise InterruptedWriteError(label=kwargs["label"], interrupt_after_seconds=0.05)

    client, _ = _make_client(tmp_path)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: InterruptingSerializer(),
    )
    try:
        session_id = "31111111-2222-3333-4444-555555555555"
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
                    "source_path": "/tmp/interrupted-ingest.jsonl",
                    "source_offset": 0,
                    "raw_json": '{"type":"assistant","text":"hi"}',
                }
            ],
        }
        response = client.post(
            "/agents/ingest",
            json=payload,
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(
                    {
                        "schema": "ship_trace.v1",
                        "trace_id": f"{session_id}:0:64:1778220000000",
                        "provider": "codex",
                        "session_id": session_id,
                        "work_context": work_context,
                    },
                    separators=(",", ":"),
                ),
            },
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.headers["Retry-After"] == expected_retry_after
        assert response.headers["X-Ingest-Lane"] == expected_lane
        assert response.headers["X-Ingest-Backpressure"] == expected_kind
        assert response.headers["X-Ingest-Error-Kind"] == expected_kind
        assert response.headers["X-Ingest-Interrupted-Label"] == _write_serializer_label_for_ship_trace(
            {"work_context": work_context}
        )
        assert response.headers["X-Ingest-Admission-State"] == "writer_interrupted"
    finally:
        api_app.dependency_overrides.clear()


def test_archive_primary_prepare_is_bounded_to_archive_sub_batches(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_LEGACY_RAW_WRITE_ENABLED", "1")
    prepared_sizes: list[int] = []

    async def fake_prepare(*, data, fallback_db, settings):  # noqa: ARG001
        prepared_sizes.append(max(len(data.events), len(data.source_lines or [])))
        return SimpleNamespace(error=None, chunks=(), records_written=0)

    monkeypatch.setattr(
        "zerg.routers.agents_ingest._prepare_archive_primary_before_ingest",
        fake_prepare,
    )
    client, _ = _make_client(tmp_path)
    try:
        session_id = "41111111-2222-3333-4444-555555555555"
        payload = {
            "id": session_id,
            "provider": "codex",
            "environment": "test",
            "project": "zerg",
            "started_at": "2026-01-01T00:00:00Z",
            "events": [
                {
                    "role": "assistant",
                    "content_text": f"hi {idx}",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "source_path": "/tmp/archive-primary-batched.jsonl",
                    "source_offset": idx,
                    "raw_json": json.dumps({"type": "assistant", "text": f"hi {idx}"}),
                }
                for idx in range(130)
            ],
            "source_lines": [
                {
                    "source_path": "/tmp/archive-primary-batched.jsonl",
                    "source_offset": idx,
                    "raw_json": json.dumps({"type": "assistant", "text": f"hi {idx}"}),
                }
                for idx in range(130)
            ],
        }
        response = client.post(
            "/agents/ingest",
            json=payload,
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(
                    {
                        "schema": "ship_trace.v1",
                        "trace_id": f"{session_id}:0:8192:1778220000000",
                        "provider": "codex",
                        "session_id": session_id,
                        "work_context": "spool_replay",
                    },
                    separators=(",", ":"),
                ),
            },
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.headers["X-Ingest-Sub-Batches"] == "9"
        assert response.headers["X-Ingest-Archive-Primary"] == "written"
        assert prepared_sizes == [16, 16, 16, 16, 16, 16, 16, 16, 2]
    finally:
        api_app.dependency_overrides.clear()


def _batched_archive_primary_payload(session_id: str, count: int) -> dict:
    return {
        "id": session_id,
        "provider": "codex",
        "environment": "test",
        "project": "zerg",
        "started_at": "2026-01-01T00:00:00Z",
        "events": [
            {
                "role": "assistant",
                "content_text": f"hi {idx}",
                "timestamp": "2026-01-01T00:00:01Z",
                "source_path": "/tmp/archive-primary-batched.jsonl",
                "source_offset": idx,
                "raw_json": json.dumps({"type": "assistant", "text": f"hi {idx}"}),
            }
            for idx in range(count)
        ],
        "source_lines": [
            {
                "source_path": "/tmp/archive-primary-batched.jsonl",
                "source_offset": idx,
                "raw_json": json.dumps({"type": "assistant", "text": f"hi {idx}"}),
            }
            for idx in range(count)
        ],
    }


def _spool_replay_trace_header(session_id: str) -> dict[str, str]:
    return {
        "X-Agents-Token": "dev",
        "X-Longhouse-Ship-Trace": json.dumps(
            {
                "schema": "ship_trace.v1",
                "trace_id": f"{session_id}:0:8192:1778220000000",
                "provider": "codex",
                "session_id": session_id,
                "work_context": "spool_replay",
            },
            separators=(",", ":"),
        ),
    }


def _live_transcript_trace_header(session_id: str) -> dict[str, str]:
    return {
        "X-Agents-Token": "dev",
        "X-Longhouse-Ship-Trace": json.dumps(
            {
                "schema": "ship_trace.v1",
                "trace_id": f"{session_id}:0:8192:1778220000000",
                "provider": "codex",
                "session_id": session_id,
                "work_context": "live_transcript",
            },
            separators=(",", ":"),
        ),
    }


def test_live_transcript_ingest_uses_cooperative_sub_batches(tmp_path):
    client, SessionLocal = _make_client(tmp_path)
    try:
        session_id = "71111111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 130),
            headers=_live_transcript_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.headers["X-Ingest-Lane"] == "live"
        assert response.headers["X-Ingest-Label"] == "ingest-live"
        assert response.headers["X-Ingest-Sub-Batches"] == "9"
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 130
    finally:
        api_app.dependency_overrides.clear()


def test_july8_archive_wal_degradation_sheds_replay_but_live_transcript_still_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_INGEST_WAL_SHED_BYTES", "100")
    monkeypatch.setattr("zerg.database.get_wal_bytes", lambda: 100)
    client, SessionLocal = _make_client(tmp_path)
    try:
        archive_session_id = "72111111-2222-3333-4444-555555555555"
        archive_response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(archive_session_id, 1),
            headers=_spool_replay_trace_header(archive_session_id),
        )
        assert archive_response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert archive_response.headers["X-Ingest-Admission-State"] == "archive_wal_pressure"
        assert archive_response.headers["X-Ingest-Backpressure"] == "archive_ingest_backpressure"

        live_session_id = "73111111-2222-3333-4444-555555555555"
        live_response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(live_session_id, 1),
            headers=_live_transcript_trace_header(live_session_id),
        )
        assert live_response.status_code == status.HTTP_200_OK, live_response.text
        assert live_response.headers["X-Ingest-Lane"] == "live"
        assert live_response.headers["X-Ingest-Label"] == "ingest-live"
        assert live_response.headers["X-Ingest-Admission-State"] == "not_applicable"
        assert "X-Ingest-Backpressure" not in live_response.headers
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == archive_session_id).count() == 0
            assert db.query(AgentEvent).filter(AgentEvent.session_id == live_session_id).count() == 1
    finally:
        api_app.dependency_overrides.clear()


def test_archive_primary_later_batch_prepare_failure_falls_back_when_legacy_raw_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_LEGACY_RAW_WRITE_ENABLED", "1")
    prepared_sizes: list[int] = []

    async def fake_prepare(*, data, fallback_db, settings):  # noqa: ARG001
        prepared_sizes.append(max(len(data.events), len(data.source_lines or [])))
        if len(prepared_sizes) == 2:
            return SimpleNamespace(error="synthetic_prepare_failure", chunks=(), records_written=0)
        return SimpleNamespace(error=None, chunks=(), records_written=0)

    monkeypatch.setattr(
        "zerg.routers.agents_ingest._prepare_archive_primary_before_ingest",
        fake_prepare,
    )
    client, SessionLocal = _make_client(tmp_path)
    try:
        session_id = "51111111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 65),
            headers=_spool_replay_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "fallback"
        assert response.headers["X-Ingest-Legacy-Raw"] == "enabled"
        assert response.headers["X-Ingest-Sub-Batches"] == "5"
        assert prepared_sizes == [16, 16, 16, 16, 1]
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 65
    finally:
        api_app.dependency_overrides.clear()


def test_archive_primary_prepare_timeout_falls_back_when_legacy_raw_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_LEGACY_RAW_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_INGEST_REQUEST_BUDGET_SECONDS", "2.0")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_INGEST_WAL_SHED_BYTES", "0")

    async def admit_without_shared_pressure(_write_label, _response):
        return False

    async def slow_prepare(*, data, fallback_db, settings):  # noqa: ARG001
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "zerg.routers.agents_ingest._acquire_archive_ingest_slot",
        admit_without_shared_pressure,
    )
    monkeypatch.setattr(
        "zerg.routers.agents_ingest._prepare_archive_primary_before_ingest",
        slow_prepare,
    )
    client, SessionLocal = _make_client(tmp_path)
    try:
        session_id = "59111111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 1),
            headers=_spool_replay_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "fallback"
        assert response.headers["X-Ingest-Legacy-Raw"] == "enabled"
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 1
    finally:
        api_app.dependency_overrides.clear()


def test_archive_primary_later_batch_prepare_failure_fails_closed_without_legacy_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_LEGACY_RAW_WRITE_ENABLED", "0")
    monkeypatch.delenv("LONGHOUSE_DISABLE_LEGACY_RAW_WRITES", raising=False)
    prepared_sizes: list[int] = []

    async def fake_prepare(*, data, fallback_db, settings):  # noqa: ARG001
        prepared_sizes.append(max(len(data.events), len(data.source_lines or [])))
        if len(prepared_sizes) == 2:
            return SimpleNamespace(error="synthetic_prepare_failure", chunks=(), records_written=0)
        return SimpleNamespace(error=None, chunks=(), records_written=0)

    monkeypatch.setattr(
        "zerg.routers.agents_ingest._prepare_archive_primary_before_ingest",
        fake_prepare,
    )
    client, SessionLocal = _make_client(tmp_path)
    try:
        session_id = "61111111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 65),
            headers=_spool_replay_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.headers["X-Ingest-Archive-Primary"] == "failed"
        assert prepared_sizes == [16, 16]
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 16
    finally:
        api_app.dependency_overrides.clear()


def test_live_archive_primary_prepare_failure_forces_legacy_raw_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_LEGACY_RAW_WRITE_ENABLED", "0")
    monkeypatch.delenv("LONGHOUSE_DISABLE_LEGACY_RAW_WRITES", raising=False)
    prepared_sizes: list[int] = []

    async def fake_prepare(*, data, fallback_db, settings):  # noqa: ARG001
        prepared_sizes.append(max(len(data.events), len(data.source_lines or [])))
        if len(prepared_sizes) == 2:
            return SimpleNamespace(error="synthetic_prepare_failure", chunks=(), records_written=0)
        return SimpleNamespace(error=None, chunks=(), records_written=0)

    monkeypatch.setattr(
        "zerg.routers.agents_ingest._prepare_archive_primary_before_ingest",
        fake_prepare,
    )
    client, SessionLocal = _make_client(tmp_path)
    try:
        session_id = "62111111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 65),
            headers=_live_transcript_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "fallback"
        assert response.headers["X-Ingest-Legacy-Raw"] == "enabled"
        assert prepared_sizes == [16, 16, 16, 16, 1]
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 65
            source_lines = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).all()
            assert any(line.raw_json_z is not None for line in source_lines)
    finally:
        api_app.dependency_overrides.clear()


def test_live_archive_primary_manifest_failure_rolls_back_before_forced_legacy_raw_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_LEGACY_RAW_WRITE_ENABLED", "0")
    monkeypatch.delenv("LONGHOUSE_DISABLE_LEGACY_RAW_WRITES", raising=False)
    rollbacks = 0

    async def fake_prepare(*, data, fallback_db, settings):  # noqa: ARG001
        chunk = SimpleNamespace(
            tenant_id="tenant-test",
            session_id=str(data.id),
            stream="source_lines",
            relative_path="tenants/tenant-test/sessions/test/chunks/source_lines.jsonl.zst",
            first_source_seq=1,
            last_source_seq=1,
            record_count=1,
            uncompressed_bytes=1,
            compressed_bytes=1,
            payload_sha256="0" * 64,
            file_sha256="1" * 64,
        )
        return SimpleNamespace(error=None, chunks=(chunk,), records_written=1)

    def fake_insert_archive_chunk_manifests(db, chunks):  # noqa: ARG001
        db.execute(text("CREATE TABLE IF NOT EXISTS poisoned_archive_write (id integer primary key)"))
        db.execute(text("INSERT INTO poisoned_archive_write (id) VALUES (1)"))
        raise RuntimeError("synthetic_manifest_failure")

    original_rollback = Session.rollback

    def observed_rollback(self):
        nonlocal rollbacks
        rollbacks += 1
        return original_rollback(self)

    monkeypatch.setattr(
        "zerg.routers.agents_ingest._prepare_archive_primary_before_ingest",
        fake_prepare,
    )
    monkeypatch.setattr(
        "zerg.services.archive_shadow.insert_archive_chunk_manifests",
        fake_insert_archive_chunk_manifests,
    )
    monkeypatch.setattr(Session, "rollback", observed_rollback)
    client, SessionLocal = _make_client(tmp_path)
    try:
        session_id = "63111111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 1),
            headers=_live_transcript_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "fallback"
        assert response.headers["X-Ingest-Legacy-Raw"] == "enabled"
        assert rollbacks >= 1
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 1
            assert db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).one().raw_json_z is not None
            assert db.execute(text("SELECT count(*) FROM poisoned_archive_write")).scalar_one() == 0
    finally:
        api_app.dependency_overrides.clear()


def test_live_archive_primary_manifest_failure_rolls_back_before_legacy_raw_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_PRIMARY_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_LEGACY_RAW_WRITE_ENABLED", "1")
    rollbacks = 0

    async def fake_prepare(*, data, fallback_db, settings):  # noqa: ARG001
        chunk = SimpleNamespace(
            tenant_id="tenant-test",
            session_id=str(data.id),
            stream="source_lines",
            relative_path="tenants/tenant-test/sessions/test/chunks/source_lines-fallback.jsonl.zst",
            first_source_seq=1,
            last_source_seq=1,
            record_count=1,
            uncompressed_bytes=1,
            compressed_bytes=1,
            payload_sha256="2" * 64,
            file_sha256="3" * 64,
        )
        return SimpleNamespace(error=None, chunks=(chunk,), records_written=1)

    def fake_insert_archive_chunk_manifests(db, chunks):  # noqa: ARG001
        db.execute(text("CREATE TABLE IF NOT EXISTS poisoned_archive_write (id integer primary key)"))
        db.execute(text("INSERT INTO poisoned_archive_write (id) VALUES (1)"))
        raise RuntimeError("synthetic_manifest_failure")

    original_rollback = Session.rollback

    def observed_rollback(self):
        nonlocal rollbacks
        rollbacks += 1
        return original_rollback(self)

    monkeypatch.setattr(
        "zerg.routers.agents_ingest._prepare_archive_primary_before_ingest",
        fake_prepare,
    )
    monkeypatch.setattr(
        "zerg.services.archive_shadow.insert_archive_chunk_manifests",
        fake_insert_archive_chunk_manifests,
    )
    monkeypatch.setattr(Session, "rollback", observed_rollback)
    client, SessionLocal = _make_client(tmp_path)
    try:
        session_id = "64111111-2222-3333-4444-555555555555"
        response = client.post(
            "/agents/ingest",
            json=_batched_archive_primary_payload(session_id, 1),
            headers=_live_transcript_trace_header(session_id),
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "fallback"
        assert response.headers["X-Ingest-Legacy-Raw"] == "enabled"
        assert rollbacks >= 1
        with SessionLocal() as db:
            assert db.query(AgentEvent).filter(AgentEvent.session_id == session_id).count() == 1
            assert db.execute(text("SELECT count(*) FROM poisoned_archive_write")).scalar_one() == 0
    finally:
        api_app.dependency_overrides.clear()


def test_agents_ingest_releases_request_db_before_serialized_write(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/ingest_release.db", pool_size=1, max_overflow=0)
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    observations: dict[str, int] = {}

    class ReleaseCheckingSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 0

        async def execute_after_closing_request_session(self, fn, fallback_db, **_kwargs):
            observations["before_close"] = engine.pool.checkedout()
            fallback_db.close()
            observations["after_close"] = engine.pool.checkedout()
            with factory() as write_db:
                result = fn(write_db)
                write_db.commit()
                return result

        async def execute(self, fn, **_kwargs):
            with factory() as write_db:
                result = fn(write_db)
                write_db.commit()
                return result

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("ingest must release the request DB before waiting on serialized writes")

    def override_db():
        db = factory()
        try:
            db.execute(text("SELECT 1"))
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="ingest-release", id="token-1", owner_id=1)

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: ReleaseCheckingSerializer(),
    )
    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        response = TestClient(api_app).post(
            "/agents/ingest",
            json={
                "id": "31111111-2222-3333-4444-555555555555",
                "provider": "codex",
                "environment": "test",
                "project": "zerg",
                "started_at": "2026-01-01T00:00:00Z",
                "events": [
                    {
                        "role": "assistant",
                        "content_text": "hi",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "source_path": "/tmp/ingest-release.jsonl",
                        "source_offset": 0,
                        "raw_json": '{"type":"assistant","text":"hi"}',
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )
        assert response.status_code == 200, response.text
    finally:
        api_app.dependency_overrides.clear()
        engine.dispose()

    assert observations == {"before_close": 1, "after_close": 0}


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
