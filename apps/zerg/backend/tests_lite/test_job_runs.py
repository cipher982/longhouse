"""Tests for job run persistence and history endpoints.

Covers:
- emit_job_run() persistence and error handling
- cleanup_old_job_runs() retention management
- GET /api/jobs/runs/recent (dashboard feed)
- GET /api/jobs/{job_id}/runs (per-job history)
- GET /api/jobs/runs/last (latest run per job)

Uses in-memory SQLite with inline setup (no shared conftest).
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.models import JobRun, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    """Create a SQLite DB with all tables, return session factory."""
    db_path = tmp_path / "test_runs.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_runs(db, job_id, count, status="success", base_time=None):
    """Insert N job runs for testing. Returns the created runs."""
    if base_time is None:
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    runs = []
    for i in range(count):
        ts = base_time + timedelta(hours=i)
        run = JobRun(
            id=str(uuid4()),
            job_id=job_id,
            status=status,
            started_at=ts,
            finished_at=ts + timedelta(minutes=1),
            duration_ms=60000,
            created_at=ts,
        )
        db.add(run)
        runs.append(run)
    db.commit()
    return runs


def _make_client(db_session):
    """Create TestClient with dependency overrides. Same pattern as test_job_preflight."""
    from zerg.dependencies.auth import require_admin
    from zerg.main import api_app, app

    admin = db_session.query(User).filter(User.email == "dev@local").first()
    if not admin:
        admin = User(email="dev@local", role="ADMIN")
        db_session.add(admin)
        db_session.commit()
        db_session.refresh(admin)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_require_admin():
        return admin

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[require_admin] = override_require_admin

    client = TestClient(app, backend="asyncio")
    return client, api_app, admin


def _mock_db_session(session_factory):
    """Build a db_session context manager that uses the given session factory."""

    @contextmanager
    def _session():
        session = session_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return _session


# ---------------------------------------------------------------------------
# emit_job_run tests
# ---------------------------------------------------------------------------


def test_emit_job_run_persists_record(tmp_path):
    """emit_job_run() should persist a JobRun record to the database."""
    SessionLocal = _make_db(tmp_path)

    with patch("zerg.database.db_session", _mock_db_session(SessionLocal)):
        from zerg.jobs.ops_db import emit_job_run

        asyncio.get_event_loop().run_until_complete(
            emit_job_run(
                job_id="test-job",
                status="success",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
                duration_ms=60000,
                error_message=None,
                tags=["test"],
                project="test-project",
            )
        )

    with SessionLocal() as db:
        runs = db.query(JobRun).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.job_id == "test-job"
        assert run.status == "success"
        assert run.duration_ms == 60000
        assert run.error_message is None
        # Tags and project stored in metadata_json
        meta = json.loads(run.metadata_json)
        assert meta["tags"] == ["test"]
        assert meta["project"] == "test-project"


def test_emit_job_run_stores_error(tmp_path):
    """emit_job_run() should store error_message for failed jobs."""
    SessionLocal = _make_db(tmp_path)

    with patch("zerg.database.db_session", _mock_db_session(SessionLocal)):
        from zerg.jobs.ops_db import emit_job_run

        asyncio.get_event_loop().run_until_complete(
            emit_job_run(
                job_id="failing-job",
                status="failure",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
                duration_ms=5000,
                error_message="boom: connection refused",
            )
        )

    with SessionLocal() as db:
        runs = db.query(JobRun).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.job_id == "failing-job"
        assert run.status == "failure"
        assert run.error_message == "boom: connection refused"


def test_emit_job_run_nonfatal_on_db_error():
    """emit_job_run() should not raise even if DB fails."""
    with patch("zerg.database.db_session", side_effect=Exception("DB down")):
        from zerg.jobs.ops_db import emit_job_run

        # Should not raise
        asyncio.get_event_loop().run_until_complete(
            emit_job_run(
                job_id="test",
                status="failure",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                duration_ms=100,
            )
        )


def test_emit_job_run_metadata_combined(tmp_path):
    """emit_job_run() should combine tags, project, scheduler, and extra metadata."""
    SessionLocal = _make_db(tmp_path)

    with patch("zerg.database.db_session", _mock_db_session(SessionLocal)):
        from zerg.jobs.ops_db import emit_job_run

        asyncio.get_event_loop().run_until_complete(
            emit_job_run(
                job_id="meta-job",
                status="success",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
                duration_ms=60000,
                tags=["alpha", "beta"],
                project="my-proj",
                scheduler="sched-abc",
                metadata={"custom_key": "custom_val"},
            )
        )

    with SessionLocal() as db:
        run = db.query(JobRun).first()
        meta = json.loads(run.metadata_json)
        assert meta["tags"] == ["alpha", "beta"]
        assert meta["project"] == "my-proj"
        assert meta["scheduler"] == "sched-abc"
        assert meta["custom_key"] == "custom_val"


# ---------------------------------------------------------------------------
# cleanup_old_job_runs tests
# ---------------------------------------------------------------------------


def test_cleanup_removes_old_runs(tmp_path):
    """cleanup_old_job_runs() should delete runs older than max_age_days."""
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        # Old runs: 60 days ago
        old_time = datetime.now(timezone.utc) - timedelta(days=60)
        _seed_runs(db, "job-a", 5, base_time=old_time)
        # Recent runs: 5 days ago
        recent_time = datetime.now(timezone.utc) - timedelta(days=5)
        _seed_runs(db, "job-a", 3, base_time=recent_time)

    with patch("zerg.database.db_session", _mock_db_session(SessionLocal)):
        from zerg.jobs.ops_db import cleanup_old_job_runs

        deleted = cleanup_old_job_runs(max_age_days=30)

    assert deleted >= 5  # At least the 5 old runs

    with SessionLocal() as db:
        remaining = db.query(JobRun).all()
        assert len(remaining) == 3  # Only recent runs kept


def test_cleanup_respects_max_per_job(tmp_path):
    """cleanup_old_job_runs() should keep at most max_per_job per job."""
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        # All recent (within 30 days)
        recent_time = datetime.now(timezone.utc) - timedelta(days=1)
        _seed_runs(db, "job-a", 15, base_time=recent_time)

    with patch("zerg.database.db_session", _mock_db_session(SessionLocal)):
        from zerg.jobs.ops_db import cleanup_old_job_runs

        deleted = cleanup_old_job_runs(max_age_days=30, max_per_job=10)

    assert deleted >= 5  # At least 5 trimmed

    with SessionLocal() as db:
        remaining = db.query(JobRun).count()
        assert remaining == 10


def test_cleanup_nonfatal_on_error():
    """cleanup_old_job_runs() should return 0 on error, not raise."""
    with patch("zerg.database.db_session", side_effect=Exception("DB down")):
        from zerg.jobs.ops_db import cleanup_old_job_runs

        result = cleanup_old_job_runs()
        assert result == 0


# ---------------------------------------------------------------------------
# HTTP tests: GET /api/jobs/runs/recent
# ---------------------------------------------------------------------------


def test_recent_runs_returns_data(tmp_path):
    """GET /runs/recent returns all runs with correct total."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_runs(db, "job-a", 5)
        _seed_runs(db, "job-b", 3)
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/runs/recent?limit=25")
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 8
            assert len(body["runs"]) == 8
            # Verify ordering: most recent first
            created_ats = [r["created_at"] for r in body["runs"]]
            assert created_ats == sorted(created_ats, reverse=True)
        finally:
            api_app_ref.dependency_overrides = {}


def test_recent_runs_respects_limit(tmp_path):
    """GET /runs/recent?limit=5 returns only 5 runs but total reflects all."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_runs(db, "job-a", 20)
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/runs/recent?limit=5")
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 20
            assert len(body["runs"]) == 5
        finally:
            api_app_ref.dependency_overrides = {}


def test_recent_runs_empty(tmp_path):
    """GET /runs/recent returns empty list when no runs exist."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/runs/recent")
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 0
            assert body["runs"] == []
        finally:
            api_app_ref.dependency_overrides = {}


# ---------------------------------------------------------------------------
# HTTP tests: GET /api/jobs/{job_id}/runs
# ---------------------------------------------------------------------------


def test_job_runs_filters_by_job(tmp_path):
    """GET /{job_id}/runs only returns runs for that job."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_runs(db, "job-a", 5)
        _seed_runs(db, "job-b", 3)
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/job-a/runs")
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 5
            assert all(r["job_id"] == "job-a" for r in body["runs"])
        finally:
            api_app_ref.dependency_overrides = {}


def test_job_runs_pagination(tmp_path):
    """GET /{job_id}/runs supports limit and offset."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_runs(db, "job-a", 10)
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/job-a/runs?limit=3&offset=0")
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 10
            assert len(body["runs"]) == 3
        finally:
            api_app_ref.dependency_overrides = {}


def test_job_runs_pagination_offset(tmp_path):
    """GET /{job_id}/runs with offset skips earlier runs."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_runs(db, "job-a", 10)
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                # Get first page
                resp1 = client.get("/api/jobs/job-a/runs?limit=3&offset=0")
                # Get second page
                resp2 = client.get("/api/jobs/job-a/runs?limit=3&offset=3")

            body1 = resp1.json()
            body2 = resp2.json()

            # Same total, different runs
            assert body1["total"] == body2["total"] == 10
            ids1 = {r["id"] for r in body1["runs"]}
            ids2 = {r["id"] for r in body2["runs"]}
            assert ids1.isdisjoint(ids2), "Pages should not overlap"
        finally:
            api_app_ref.dependency_overrides = {}


def test_job_runs_nonexistent_job(tmp_path):
    """GET /{job_id}/runs returns empty for nonexistent job."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/nonexistent/runs")
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 0
            assert body["runs"] == []
        finally:
            api_app_ref.dependency_overrides = {}


# ---------------------------------------------------------------------------
# HTTP tests: GET /api/jobs/runs/last
# ---------------------------------------------------------------------------


def test_last_runs_per_job(tmp_path):
    """GET /runs/last returns exactly one run per job."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        _seed_runs(db, "job-a", 5)
        _seed_runs(db, "job-b", 3)
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/runs/last")
            assert resp.status_code == 200
            body = resp.json()
            assert "job-a" in body["last_runs"]
            assert "job-b" in body["last_runs"]
            assert body["last_runs"]["job-a"]["job_id"] == "job-a"
            assert body["last_runs"]["job-b"]["job_id"] == "job-b"
        finally:
            api_app_ref.dependency_overrides = {}


def test_last_runs_empty(tmp_path):
    """GET /runs/last returns empty dict when no runs exist."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/runs/last")
            assert resp.status_code == 200
            assert resp.json()["last_runs"] == {}
        finally:
            api_app_ref.dependency_overrides = {}


def test_last_runs_returns_most_recent(tmp_path):
    """GET /runs/last should return the chronologically latest run per job."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        runs = _seed_runs(db, "job-a", 5)
        client, api_app_ref, _ = _make_client(db)
        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.get("/api/jobs/runs/last")
            body = resp.json()
            last_run = body["last_runs"]["job-a"]
            # The last seeded run (index 4) has the latest created_at
            assert last_run["id"] == runs[-1].id
        finally:
            api_app_ref.dependency_overrides = {}
