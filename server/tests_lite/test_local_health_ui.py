from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services import local_health_ui
from zerg.services.shipper.service import Platform


def test_build_local_health_command_includes_current_python_and_claude_dir():
    command = local_health_ui.build_local_health_command(claude_dir="/tmp/claude")

    assert "zerg.cli.main local-health --json" in command
    assert "--claude-dir" in command
    assert "/tmp/claude" in command


def test_default_install_menubar_respects_env(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_INSTALL_MENUBAR", "0")
    assert local_health_ui.default_install_menubar() is False

    monkeypatch.setenv("LONGHOUSE_INSTALL_MENUBAR", "1")
    assert local_health_ui.default_install_menubar() is True


def test_install_menubar_service_writes_plist_and_loads(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(local_health_ui, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(
        local_health_ui,
        "ensure_runtime_artifact",
        lambda component, source_override=None: SimpleNamespace(
            path="/Users/test/Applications/Longhouse.app",
            launch_path="/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            source="override",
            installed_now=True,
        ),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, check=False):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(local_health_ui.subprocess, "run", fake_run)

    result = local_health_ui.install_menubar_service(
        ui_url="https://longhouse.ai",
        claude_dir=str(home / ".claude"),
    )

    plist_path = home / "Library" / "LaunchAgents" / "com.longhouse.local-health-menubar.plist"
    assert plist_path.exists()
    plist = plist_path.read_text(encoding="utf-8")
    assert "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse" in plist
    assert "--health-command" in plist
    assert "https://longhouse.ai" in plist
    assert calls[-1] == ["launchctl", "load", str(plist_path)]
    assert result["plist_path"] == str(plist_path)
    assert result["app_path"] == "/Users/test/Applications/Longhouse.app"
    assert result["launch_path"] == "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse"


def test_get_menubar_service_status_returns_not_installed_when_plist_missing(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(local_health_ui, "detect_platform", lambda: Platform.MACOS)

    assert local_health_ui.get_menubar_service_status() == "not-installed"


def test_get_menubar_service_info_includes_app_bundle_details(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(local_health_ui, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(local_health_ui, "get_menubar_service_status", lambda: "running")
    monkeypatch.setattr(
        local_health_ui,
        "resolve_installed_runtime_artifact",
        lambda component: SimpleNamespace(
            component=local_health_ui.RuntimeComponent.LOCAL_HEALTH_APP,
            path="/Users/test/Applications/Longhouse.app",
            launch_path="/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        ) if component == local_health_ui.RuntimeComponent.LOCAL_HEALTH_APP else None,
    )

    info = local_health_ui.get_menubar_service_info()

    assert info["status"] == "running"
    assert info["artifact_path"] == "/Users/test/Applications/Longhouse.app"
    assert info["launch_path"] == "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse"
    assert info["runtime_mode"] == "app-bundle"
