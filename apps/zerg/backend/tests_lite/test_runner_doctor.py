from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.models.models import Runner
from zerg.models.models import User


TEST_ENV = {
    "FERNET_SECRET": "test-fernet-secret",
    "AUTH_DISABLED": "1",
    "JWT_SECRET": "test-jwt-secret-1234",
    "INTERNAL_API_SECRET": "test-internal-secret-1234",
}


def _make_client(tmp_path: Path):
    db_path = tmp_path / "runner-doctor.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    with patch.dict(os.environ, {**TEST_ENV, "DATABASE_URL": f"sqlite:///{db_path}"}, clear=False):
        from zerg.main import api_app

        db = SessionLocal()
        user = User(email="doctor@test.local", role="ADMIN")
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


def test_runner_doctor_reports_healthy_online_runner(tmp_path: Path):
    client, api_app, db, user = _make_client(tmp_path)
    try:
        runner = Runner(
            owner_id=user.id,
            name="clifford",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            runner_metadata={
                "hostname": "clifford",
                "platform": "linux",
                "runner_version": "0.1.2",
                "install_mode": "server",
                "capabilities": ["exec.full"],
            },
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        response = client.get(f"/runners/{runner.id}/doctor")
        assert response.status_code == 200
        payload = response.json()
        assert payload["severity"] == "healthy"
        assert payload["reason_code"] == "healthy"
        assert payload["repair_supported"] is False
        assert payload["repair_install_mode"] == "server"
    finally:
        api_app.dependency_overrides.clear()
        db.close()


def test_runner_doctor_flags_capability_mismatch(tmp_path: Path):
    client, api_app, db, user = _make_client(tmp_path)
    try:
        runner = Runner(
            owner_id=user.id,
            name="cube",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            runner_metadata={
                "hostname": "cube",
                "platform": "linux",
                "install_mode": "server",
                "capabilities": ["exec.readonly"],
            },
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        response = client.get(f"/runners/{runner.id}/doctor")
        assert response.status_code == 200
        payload = response.json()
        assert payload["severity"] == "error"
        assert payload["reason_code"] == "runner_capabilities_mismatch"
        assert payload["repair_supported"] is True
    finally:
        api_app.dependency_overrides.clear()
        db.close()


def test_runner_doctor_flags_never_connected_runner(tmp_path: Path):
    client, api_app, db, user = _make_client(tmp_path)
    try:
        runner = Runner(
            owner_id=user.id,
            name="fresh-box",
            auth_secret_hash="hash",
            capabilities=["exec.readonly"],
            status="offline",
            runner_metadata=None,
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        response = client.get(f"/runners/{runner.id}/doctor")
        assert response.status_code == 200
        payload = response.json()
        assert payload["severity"] == "error"
        assert payload["reason_code"] == "runner_never_connected"
        assert payload["repair_supported"] is True
    finally:
        api_app.dependency_overrides.clear()
        db.close()
