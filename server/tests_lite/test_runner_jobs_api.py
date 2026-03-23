from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.models.models import Runner
from zerg.models.models import RunnerJob
from zerg.models.user import User
from zerg.utils.time import utc_now_naive


TEST_ENV = {
    "FERNET_SECRET": "test-fernet-secret",
    "AUTH_DISABLED": "1",
    "JWT_SECRET": "test-jwt-secret-1234",
    "INTERNAL_API_SECRET": "test-internal-secret-1234",
}


def _make_client(tmp_path: Path):
    db_path = tmp_path / "runner-jobs.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    with patch.dict(os.environ, {**TEST_ENV, "DATABASE_URL": f"sqlite:///{db_path}"}, clear=False):
        from zerg.main import api_app

        db = SessionLocal()
        user = User(email="jobs@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        def override_get_db():
            try:
                yield db
            finally:
                pass

        def override_current_user():
            return user

        api_app.dependency_overrides[get_db] = override_get_db
        api_app.dependency_overrides[get_current_user] = override_current_user
        client = TestClient(api_app)
        return client, api_app, db, user


def test_list_runner_jobs_returns_recent_jobs_first(tmp_path: Path):
    client, api_app, db, user = _make_client(tmp_path)
    try:
        now = utc_now_naive()
        runner = Runner(
            owner_id=user.id,
            name="cinder",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            last_seen_at=now,
            runner_metadata={"capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        older = RunnerJob(
            id="job-older",
            owner_id=user.id,
            runner_id=runner.id,
            command="hostname",
            timeout_secs=30,
            status="success",
            exit_code=0,
            created_at=now - timedelta(minutes=2),
        )
        newer = RunnerJob(
            id="job-newer",
            owner_id=user.id,
            runner_id=runner.id,
            command="pwd",
            timeout_secs=30,
            status="failed",
            error="boom",
            created_at=now - timedelta(minutes=1),
        )
        db.add_all([older, newer])
        db.commit()

        response = client.get(f"/runners/{runner.id}/jobs?limit=1")
        assert response.status_code == 200
        payload = response.json()
        assert [job["id"] for job in payload["jobs"]] == ["job-newer"]
        assert payload["jobs"][0]["status"] == "failed"
    finally:
        api_app.dependency_overrides.clear()
        db.close()


def test_list_runner_jobs_requires_ownership(tmp_path: Path):
    client, api_app, db, user = _make_client(tmp_path)
    try:
        other = User(email="other@test.local", role="ADMIN")
        db.add(other)
        db.commit()
        db.refresh(other)

        runner = Runner(
            owner_id=other.id,
            name="other-runner",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            last_seen_at=utc_now_naive(),
            runner_metadata={"capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        response = client.get(f"/runners/{runner.id}/jobs")
        assert response.status_code == 404
    finally:
        api_app.dependency_overrides.clear()
        db.close()
