"""Tests for the *Run History* feature (Course model, routes, CRUD helpers)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.models.models import Course
from zerg.utils.time import utc_now_naive


def _create_basic_run(db: Session, fiche_id: int, thread_id: int) -> Course:
    """Helper: insert an Course row via the public CRUD helpers."""

    run_row = crud.create_course(db, fiche_id=fiche_id, thread_id=thread_id, trigger="manual", status="queued")
    crud.mark_course_running(db, run_row.id, started_at=utc_now_naive())
    crud.mark_course_finished(db, run_row.id)
    return run_row


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


def test_run_crud_lifecycle(db_session: Session, sample_fiche, sample_thread):
    """create_course → mark_course_running → mark_course_finished should persist correctly."""

    run_row = crud.create_course(db_session, fiche_id=sample_fiche.id, thread_id=sample_thread.id, trigger="manual")

    assert run_row.id is not None
    assert run_row.status == "queued"

    # Mark running
    running_row = crud.mark_course_running(db_session, run_row.id)
    assert running_row is not None
    assert running_row.status == "running"
    assert running_row.started_at is not None

    # Mark finished
    finished_row = crud.mark_course_finished(db_session, run_row.id)
    assert finished_row.status == "success"
    assert finished_row.finished_at is not None
    # Duration should be auto-calculated
    assert finished_row.duration_ms is not None
    assert finished_row.duration_ms >= 0

    # Listing helper should return latest first
    latest_runs = crud.list_courses(db_session, sample_fiche.id, limit=5)
    assert latest_runs[0].id == finished_row.id


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@pytest.fixture
def _seed_runs(db_session: Session, sample_fiche, sample_thread):  # noqa: D401 – fixture name starting with underscore
    """Insert a few runs for API list endpoint tests."""

    # Three runs of varying status for realism
    _create_basic_run(db_session, sample_fiche.id, sample_thread.id)
    _create_basic_run(db_session, sample_fiche.id, sample_thread.id)
    _create_basic_run(db_session, sample_fiche.id, sample_thread.id)


def test_list_courses_endpoint(client: TestClient, db_session: Session, sample_fiche, sample_thread, _seed_runs):
    """/api/fiches/{id}/runs returns newest first and respects `limit`."""

    resp = client.get(f"/api/fiches/{sample_fiche.id}/runs?limit=2")
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, list)
    assert len(payload) == 2

    # Ensure ordering DESC by id
    assert payload[0]["id"] > payload[1]["id"]


def test_get_run_endpoint(client: TestClient, db_session: Session, sample_fiche, sample_thread):
    """/api/runs/{course_id} returns the row or 404."""

    run_row = _create_basic_run(db_session, sample_fiche.id, sample_thread.id)

    resp_ok = client.get(f"/api/runs/{run_row.id}")
    assert resp_ok.status_code == 200
    assert resp_ok.json()["id"] == run_row.id

    resp_404 = client.get("/api/runs/999999")
    assert resp_404.status_code == 404


# ---------------------------------------------------------------------------
# Integration – execute task and verify run recorded + events emitted
# ---------------------------------------------------------------------------


def test_task_run_creates_course(client: TestClient, db_session: Session, sample_fiche):
    """Full stack: POST /fiches/{id}/task → Course row + COURSE events."""

    from zerg.events import EventType
    from zerg.events.event_bus import event_bus

    collected: list = []

    async def _handler(data):  # noqa: D401 – simple collector
        collected.append(data)

    for et in (EventType.COURSE_CREATED, EventType.COURSE_UPDATED):
        event_bus.subscribe(et, _handler)

    handler = _handler  # keep reference for unsubscribe later

    # Trigger the task run
    resp = client.post(f"/api/fiches/{sample_fiche.id}/task")
    assert resp.status_code == 202

    # After route returns, run should be completed (FicheRunner stub is fast)
    runs = crud.list_courses(db_session, sample_fiche.id, limit=1)
    assert runs, "Run row not created"
    latest_run = runs[0]
    assert latest_run.status == "success"

    # We expect at least: COURSE_CREATED, COURSE_UPDATED (running), COURSE_UPDATED (success)
    # Allow small scheduling differences – check counts and statuses
    statuses = [evt.get("status") for evt in collected if evt.get("fiche_id") == sample_fiche.id]
    assert "queued" in statuses
    assert "running" in statuses
    assert "success" in statuses

    # Clean up: unsubscribe handler to avoid leaking into other tests
    for et in [EventType.COURSE_CREATED, EventType.COURSE_UPDATED]:
        event_bus.unsubscribe(et, handler)
