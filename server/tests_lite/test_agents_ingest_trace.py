from __future__ import annotations

import json
import os

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

from zerg.routers.agents_ingest import _ARCHIVE_INGEST_MAX_IN_FLIGHT
from zerg.routers.agents_ingest import _acquire_archive_ingest_slot
from zerg.routers.agents_ingest import _archive_retry_after_for_queue_depth
from zerg.routers.agents_ingest import _check_historical_archive_admission
from zerg.routers.agents_ingest import _check_live_ingest_writer_pressure
from zerg.routers.agents_ingest import _incremental_session_counts_for_label
from zerg.routers.agents_ingest import _ingest_lane_for_label
from zerg.routers.agents_ingest import _release_archive_ingest_slot
from zerg.routers.agents_ingest import _stage_timing_header_value
from zerg.routers.agents_ingest import _sync_derived_projections_for_label
from zerg.routers.agents_ingest import _sync_session_counts_for_label
from zerg.routers.agents_ingest import _write_serializer_label_for_ship_trace


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


def test_live_ingest_never_calls_historical_admission(monkeypatch):
    from zerg.services import historical_admission

    def forbidden(**_kwargs):
        raise AssertionError("live ingest entered historical admission")

    monkeypatch.setattr(historical_admission, "evaluate_historical_admission", forbidden)

    _check_historical_archive_admission("ingest-live", Response(), admitted_bytes=1024)


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
