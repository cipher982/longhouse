"""Tests for the served runner install script contract."""

from __future__ import annotations

import os
from pathlib import Path
from subprocess import run
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


def _settings(**overrides):
    base = {
        "app_public_url": "https://david010.longhouse.ai",
        "testing": False,
        "runner_binary_tag": "runner-v9.9.9",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _fetch_install_script(**params):
    env = {
        "DATABASE_URL": "sqlite:///test.db",
        "FERNET_SECRET": "test-fernet-secret",
        "AUTH_DISABLED": "1",
        "JWT_SECRET": "test-jwt-secret-1234",
        "INTERNAL_API_SECRET": "test-internal-secret-1234",
    }
    with patch.dict(os.environ, env, clear=False):
        from zerg.main import app

        with patch("zerg.config.get_settings", return_value=_settings()):
            client = TestClient(app, backend="asyncio")
            return client.get("/api/runners/install.sh", params=params)


def test_install_script_defaults_to_desktop_mode_and_is_valid_bash(tmp_path):
    response = _fetch_install_script(enroll_token="token_123")

    assert response.status_code == 200
    assert 'RUNNER_INSTALL_MODE="${RUNNER_INSTALL_MODE:-desktop}"' in response.text
    assert "systemctl --user enable longhouse-runner" in response.text
    assert "For always-on servers, use RUNNER_INSTALL_MODE=server instead." in response.text

    script_path = Path(tmp_path) / "install.sh"
    script_path.write_text(response.text)
    run(["bash", "-n", str(script_path)], check=True)


def test_install_script_server_mode_exposes_system_service_contract(tmp_path):
    response = _fetch_install_script(enroll_token="token_123", mode="server")

    assert response.status_code == 200
    assert 'RUNNER_INSTALL_MODE="${RUNNER_INSTALL_MODE:-server}"' in response.text
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
