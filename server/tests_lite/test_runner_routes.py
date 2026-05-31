from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.models.models import Runner
from zerg.models.models import User


TEST_ENV = {
    "FERNET_SECRET": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "AUTH_DISABLED": "1",
    "JWT_SECRET": "test-jwt-secret-1234",
    "INTERNAL_API_SECRET": "test-internal-secret-1234",
}


def _make_client(tmp_path: Path):
    db_path = tmp_path / "runner-routes.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    with patch.dict(os.environ, {**TEST_ENV, "DATABASE_URL": f"sqlite:///{db_path}"}, clear=False):
        from zerg.main import api_app

        db = SessionLocal()
        user = User(email="runner-routes@test.local", role="ADMIN")
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


def test_delete_runner_removes_offline_runner(tmp_path: Path):
    client, api_app, db, user = _make_client(tmp_path)
    try:
        runner = Runner(
            owner_id=user.id,
            name="lh-vm-canary-20260415120000",
            auth_secret_hash="hash",
            capabilities=["exec.readonly"],
            status="offline",
            runner_metadata={"install_mode": "server"},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        with patch(
            "zerg.routers.runners.get_runner_connection_manager",
            return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: False),
        ):
            response = client.delete(f"/runners/{runner.id}")

        assert response.status_code == 204
        assert db.get(Runner, runner.id) is None
    finally:
        api_app.dependency_overrides.clear()
        db.close()


def test_delete_runner_rejects_connected_runner(tmp_path: Path):
    client, api_app, db, user = _make_client(tmp_path)
    try:
        runner = Runner(
            owner_id=user.id,
            name="demo-runner",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            runner_metadata={"install_mode": "server"},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        with patch(
            "zerg.routers.runners.get_runner_connection_manager",
            return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        ):
            response = client.delete(f"/runners/{runner.id}")

        assert response.status_code == 409
        assert response.json()["detail"] == "Cannot delete a connected runner. Wait for it to disconnect or revoke it first."
        assert db.get(Runner, runner.id) is not None
    finally:
        api_app.dependency_overrides.clear()
        db.close()
