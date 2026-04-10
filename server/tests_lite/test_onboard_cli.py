from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import onboard as onboard_cli
from zerg.cli.main import app


class _DemoResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _DemoClient:
    def __init__(self, response: _DemoResponse) -> None:
        self.response = response
        self.calls: list[str] = []

    def __enter__(self) -> _DemoClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def post(self, url: str) -> _DemoResponse:
        self.calls.append(url)
        return self.response


def test_onboard_quick_imports_existing_sessions_first(monkeypatch, tmp_path):
    runner = CliRunner()
    subprocess_calls: list[list[str]] = []

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_emit_test_event", lambda api_url: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli, "save_config", lambda config: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)

    result = runner.invoke(app, ["onboard", "--quick"])

    assert result.exit_code == 0, result.output
    assert "Install Longhouse, open it, and find one prior session." in result.output
    assert "Step 3: Bring in your existing sessions" in result.output
    assert "Importing your existing sessions now..." in result.output
    assert "[OK] Existing sessions are ready to look for in Longhouse" in result.output
    assert "Skipping demo data. Use 'longhouse serve --demo' later if you want a safe preview." in result.output
    assert "Find one prior session in the timeline" in result.output
    assert "longhouse claude" in result.output
    assert "longhouse wrap --install" not in result.output
    assert "wrapper mode" not in result.output
    assert any(
        call[:4] == ["longhouse", "connect", "--install", "--url"]
        and "--machine-name" in call
        and "--no-menubar" in call
        for call in subprocess_calls
    )
    assert ["longhouse", "ship", "--url", "http://127.0.0.1:8080"] in subprocess_calls


def test_onboard_quick_without_cli_seeds_demo_sessions(monkeypatch, tmp_path):
    runner = CliRunner()
    demo_client = _DemoClient(_DemoResponse(200, {"seeded": True, "sessions_created": 7}))
    subprocess_calls: list[list[str]] = []

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda _cmd: False)
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_emit_test_event", lambda api_url: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli, "save_config", lambda config: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli.httpx, "Client", lambda timeout=10: demo_client)

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)

    result = runner.invoke(app, ["onboard", "--quick"])

    assert result.exit_code == 0, result.output
    assert "No supported AI CLI found" in result.output
    assert "You can still set up the local runtime now and connect a CLI later." in result.output
    assert "[OK] Machine agent installed for automatic imports" in result.output
    assert "No supported CLI found yet, so Longhouse skipped the initial import." in result.output
    assert "Seeding demo sessions..." in result.output
    assert "[OK] Seeded 7 demo sessions" in result.output
    assert "Open Longhouse" in result.output
    assert demo_client.calls == ["http://127.0.0.1:8080/api/agents/demo"]
    assert any(
        call[:4] == ["longhouse", "connect", "--install", "--url"]
        and "--machine-name" in call
        and "--no-menubar" in call
        for call in subprocess_calls
    )


def test_onboard_quick_in_ci_skips_service_manager_install(monkeypatch, tmp_path):
    runner = CliRunner()
    subprocess_calls: list[list[str]] = []

    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_emit_test_event", lambda api_url: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli, "save_config", lambda config: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)

    result = runner.invoke(app, ["onboard", "--quick"])

    assert result.exit_code == 0, result.output
    assert "[--] Background machine-agent install is not available in this environment" in result.output
    assert "Use: longhouse connect" in result.output
    assert ["longhouse", "ship", "--url", "http://127.0.0.1:8080"] in subprocess_calls
    assert not any(call[:2] == ["longhouse", "connect"] for call in subprocess_calls)


def test_onboard_quick_in_ci_can_install_services_when_explicitly_enabled(monkeypatch, tmp_path):
    runner = CliRunner()
    subprocess_calls: list[list[str]] = []
    open_calls: list[str] = []

    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("LONGHOUSE_INSTALL_SERVICES_IN_CI", "1")
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_emit_test_event", lambda api_url: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: True)
    monkeypatch.setattr(onboard_cli, "save_config", lambda config: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli.webbrowser, "open", lambda url: open_calls.append(url) or True)

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)

    result = runner.invoke(app, ["onboard", "--quick"])

    assert result.exit_code == 0, result.output
    assert "[OK] Machine agent installed for automatic imports" in result.output
    assert any(
        call[:4] == ["longhouse", "connect", "--install", "--url"]
        and "--machine-name" in call
        and "--menubar" in call
        for call in subprocess_calls
    )
    assert open_calls == ["http://127.0.0.1:8080"]


def test_onboard_interactive_stays_focused_on_explicit_launch_paths(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: False)
    monkeypatch.setattr(onboard_cli, "_emit_test_event", lambda api_url: False)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli, "save_config", lambda config: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")

    result = runner.invoke(
        app,
        ["onboard", "--no-server", "--no-shipper", "--no-demo"],
        input="1\n",
    )

    assert result.exit_code == 0, result.output
    assert "Step 7: PATH verification" in result.output
    assert "longhouse claude" in result.output
    assert "longhouse wrap --install" not in result.output
    assert "wrapper mode" not in result.output
