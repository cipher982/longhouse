"""Tests for job pre-flight validation: _check_required_secrets and enable endpoint.

Covers the pre-flight secret check logic added to routers/jobs.py, including:
- Direct unit tests of _check_required_secrets
- HTTP-level tests of the enable endpoint (409, force, success)

Uses in-memory SQLite with inline setup (no shared conftest).
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.jobs.registry import JobConfig, job_registry
from zerg.models.models import JobSecret, User
from zerg.routers.jobs import _check_required_secrets


# ---------------------------------------------------------------------------
# Helpers: in-memory SQLite DB for unit tests
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    """Create an in-memory SQLite DB with all tables, return session factory."""
    db_path = tmp_path / "test_preflight.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal


def _seed_user(db, user_id=1, email="test@local"):
    """Insert a test user and return it."""
    user = User(id=user_id, email=email, role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _register_test_job(job_id="test-job", secrets=None, enabled=False):
    """Register a test job in the global registry. Returns the config."""
    if secrets is None:
        secrets = ["SECRET_A", "SECRET_B"]

    async def _noop():
        return {"ok": True}

    config = JobConfig(
        id=job_id,
        cron="0 0 * * *",
        func=_noop,
        enabled=enabled,
        secrets=secrets,
        description="Test job for preflight checks",
    )
    # Force-register (remove first if exists)
    job_registry.unregister(job_id)
    job_registry.register(config)
    return config


# ---------------------------------------------------------------------------
# Cleanup: ensure test jobs don't leak between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_registry():
    """Remove test jobs from the global registry after each test."""
    yield
    job_registry.unregister("test-job")
    job_registry.unregister("test-job-no-secrets")


# ---------------------------------------------------------------------------
# Unit tests: _check_required_secrets
# ---------------------------------------------------------------------------


def test_check_required_secrets_all_configured(tmp_path):
    """When all required secrets exist in DB, returns empty list."""
    SessionLocal = _make_db(tmp_path)
    _register_test_job("test-job", secrets=["SECRET_A", "SECRET_B"])

    with SessionLocal() as db:
        user = _seed_user(db)
        # Insert both secrets
        db.add(JobSecret(owner_id=user.id, key="SECRET_A", encrypted_value="enc_a"))
        db.add(JobSecret(owner_id=user.id, key="SECRET_B", encrypted_value="enc_b"))
        db.commit()

        missing = _check_required_secrets("test-job", user.id, db)
        assert missing == []


def test_check_required_secrets_missing(tmp_path):
    """When some required secrets are missing, returns those keys."""
    SessionLocal = _make_db(tmp_path)
    _register_test_job("test-job", secrets=["SECRET_A", "SECRET_B"])

    with SessionLocal() as db:
        user = _seed_user(db)
        # Only insert SECRET_A — SECRET_B is missing
        db.add(JobSecret(owner_id=user.id, key="SECRET_A", encrypted_value="enc_a"))
        db.commit()

        missing = _check_required_secrets("test-job", user.id, db)
        assert missing == ["SECRET_B"]


def test_check_required_secrets_env_fallback(tmp_path):
    """Secrets available via env vars count as configured."""
    SessionLocal = _make_db(tmp_path)
    _register_test_job("test-job", secrets=["SECRET_A", "SECRET_B"])

    with SessionLocal() as db:
        user = _seed_user(db)
        # SECRET_A in DB, SECRET_B in env
        db.add(JobSecret(owner_id=user.id, key="SECRET_A", encrypted_value="enc_a"))
        db.commit()

        with patch.dict(os.environ, {"SECRET_B": "from-env"}):
            missing = _check_required_secrets("test-job", user.id, db)
            assert missing == []


def test_check_required_secrets_no_config(tmp_path):
    """Unknown job_id returns empty list (no config to check against)."""
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = _seed_user(db)
        missing = _check_required_secrets("nonexistent-job", user.id, db)
        assert missing == []


# ---------------------------------------------------------------------------
# HTTP-level tests: enable endpoint (409 / force / success)
# ---------------------------------------------------------------------------


def _make_client(db_session):
    """Create a TestClient with dependency overrides targeting api_app.

    Seeds a deterministic admin user (id=1, dev@local) if not already present.
    Returns (client, api_app, admin_user).
    """
    from zerg.dependencies.auth import require_admin
    from zerg.main import api_app, app

    # Seed an admin user if not present
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


def test_enable_job_409_missing_secrets(tmp_path):
    """POST /api/jobs/{id}/enable returns 409 when secrets are missing."""
    SessionLocal = _make_db(tmp_path)
    _register_test_job("test-job", secrets=["SECRET_A", "SECRET_B"], enabled=False)

    with SessionLocal() as db:
        client, api_app_ref, admin = _make_client(db)

        try:
            # Patch _ensure_jobs_registered to skip manifest loading
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.post("/api/jobs/test-job/enable")

            assert resp.status_code == 409
            body = resp.json()
            assert "missing" in body["detail"]
            assert set(body["detail"]["missing"]) == {"SECRET_A", "SECRET_B"}
        finally:
            api_app_ref.dependency_overrides = {}


def test_enable_job_force_bypasses_check(tmp_path):
    """POST /api/jobs/{id}/enable?force=true bypasses the secret check."""
    SessionLocal = _make_db(tmp_path)
    _register_test_job("test-job", secrets=["SECRET_A", "SECRET_B"], enabled=False)

    with SessionLocal() as db:
        client, api_app_ref, admin = _make_client(db)

        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.post("/api/jobs/test-job/enable?force=true")

            assert resp.status_code == 200
            body = resp.json()
            assert body["id"] == "test-job"
            assert body["enabled"] is True
        finally:
            api_app_ref.dependency_overrides = {}


def test_enable_job_all_secrets_configured(tmp_path):
    """Enable succeeds when all secrets are present in DB."""
    SessionLocal = _make_db(tmp_path)
    _register_test_job("test-job", secrets=["SECRET_A", "SECRET_B"], enabled=False)

    with SessionLocal() as db:
        client, api_app_ref, admin = _make_client(db)
        # Insert both required secrets using the admin user's id
        db.add(JobSecret(owner_id=admin.id, key="SECRET_A", encrypted_value="enc_a"))
        db.add(JobSecret(owner_id=admin.id, key="SECRET_B", encrypted_value="enc_b"))
        db.commit()

        try:
            with patch("zerg.routers.jobs._ensure_jobs_registered"):
                resp = client.post("/api/jobs/test-job/enable")

            assert resp.status_code == 200
            body = resp.json()
            assert body["id"] == "test-job"
            assert body["enabled"] is True
        finally:
            api_app_ref.dependency_overrides = {}
