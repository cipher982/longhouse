"""Tests for the served runner install script contract."""

from __future__ import annotations

import os
from pathlib import Path
from subprocess import run
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.crud import runner_crud
from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.models.models import User


def _settings(**overrides):
    base = {
        "app_public_url": "https://david010.longhouse.ai",
        "testing": False,
        "runner_binary_tag": "runner-v9.9.9",
        "runner_docker_image": "ghcr.io/cipher982/longhouse-runner:latest",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(method: str, path: str, *, settings_overrides: dict | None = None, base_url: str = "http://testserver", **kwargs):
    env = {
        "DATABASE_URL": "sqlite:///test.db",
        "FERNET_SECRET": "test-fernet-secret",
        "AUTH_DISABLED": "1",
        "JWT_SECRET": "test-jwt-secret-1234",
        "INTERNAL_API_SECRET": "test-internal-secret-1234",
    }
    with patch.dict(os.environ, env, clear=False):
        from zerg.main import app

        with patch("zerg.config.get_settings", return_value=_settings(**(settings_overrides or {}))):
            client = TestClient(app, backend="asyncio", base_url=base_url)
            return client.request(method, path, **kwargs)


def _fetch_install_script(**params):
    return _request("GET", "/api/runners/install.sh", params=params)


def test_install_script_defaults_to_desktop_mode_and_is_valid_bash(tmp_path):
    response = _fetch_install_script(enroll_token="token_123")

    assert response.status_code == 200
    assert 'RUNNER_INSTALL_MODE="${RUNNER_INSTALL_MODE:-desktop}"' in response.text
    assert 'RUNNER_CAPABILITIES=$RUNNER_CAPABILITIES' in response.text
    assert "systemctl --user enable longhouse-runner" in response.text
    assert "For always-on servers, use RUNNER_INSTALL_MODE=server instead." in response.text

    script_path = Path(tmp_path) / "install.sh"
    script_path.write_text(response.text)
    run(["bash", "-n", str(script_path)], check=True)


def test_install_script_server_mode_exposes_system_service_contract(tmp_path):
    response = _fetch_install_script(enroll_token="token_123", mode="server")

    assert response.status_code == 200
    assert 'RUNNER_INSTALL_MODE="${RUNNER_INSTALL_MODE:-server}"' in response.text
    assert 'RUNNER_CAPABILITIES=$RUNNER_CAPABILITIES' in response.text
    assert "EnvironmentFile=/etc/longhouse/runner.env" in response.text
    assert "ExecStart=/usr/local/bin/longhouse-runner" in response.text
    assert "WantedBy=multi-user.target" in response.text
    assert "sudo systemctl status longhouse-runner" in response.text

    script_path = Path(tmp_path) / "install-server.sh"
    script_path.write_text(response.text)
    run(["bash", "-n", str(script_path)], check=True)


def test_install_script_rejects_invalid_mode():
    response = _fetch_install_script(enroll_token="token_123", mode="weird")

    assert response.status_code == 400
    assert response.text == "Error: Invalid mode (use desktop or server)"


def test_create_enroll_token_uses_request_base_url_when_public_url_missing(tmp_path):
    db_path = tmp_path / "runner-enroll.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "FERNET_SECRET": "test-fernet-secret",
        "AUTH_DISABLED": "1",
        "JWT_SECRET": "test-jwt-secret-1234",
        "INTERNAL_API_SECRET": "test-internal-secret-1234",
    }

    with patch.dict(os.environ, env, clear=False):
        from zerg.main import api_app, app

        with SessionLocal() as db:
            user = User(email="dev@local", role="ADMIN")
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
            try:
                with patch("zerg.config.get_settings", return_value=_settings(app_public_url=None)):
                    client = TestClient(app, backend="asyncio", base_url="http://127.0.0.1:43955")
                    response = client.post("/api/runners/enroll-token")
            finally:
                api_app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["longhouse_url"] == "http://127.0.0.1:43955"
    assert "http://127.0.0.1:43955/api/runners/install.sh" in payload["one_liner_install_command"]
    assert "http://127.0.0.1:43955/api/runners/register" in payload["docker_command"]



def test_register_runner_reenroll_returns_existing_capabilities(tmp_path):
    db_path = tmp_path / "runner-register.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "FERNET_SECRET": "test-fernet-secret",
        "AUTH_DISABLED": "1",
        "JWT_SECRET": "test-jwt-secret-1234",
        "INTERNAL_API_SECRET": "test-internal-secret-1234",
    }

    with patch.dict(os.environ, env, clear=False):
        from zerg.main import api_app, app

        with SessionLocal() as db:
            user = User(email="dev@local", role="ADMIN")
            db.add(user)
            db.commit()
            db.refresh(user)

            runner_crud.create_runner(
                db=db,
                owner_id=user.id,
                name="clifford",
                auth_secret="old-secret",
                capabilities=["exec.full"],
            )
            _, enroll_token = runner_crud.create_enroll_token(db=db, owner_id=user.id, ttl_minutes=10)

            def override_get_db():
                try:
                    yield db
                finally:
                    pass

            api_app.dependency_overrides[get_db] = override_get_db
            try:
                with patch("zerg.config.get_settings", return_value=_settings()):
                    client = TestClient(app, backend="asyncio")
                    response = client.post(
                        "/api/runners/register",
                        json={"enroll_token": enroll_token, "name": "clifford"},
                    )
            finally:
                api_app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "clifford"
    assert payload["runner_capabilities_csv"] == "exec.full"
