from __future__ import annotations

import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services import desktop_app
from zerg.services.shipper.service import Platform


def test_build_local_health_command_includes_current_python_and_claude_dir():
    command = desktop_app.build_local_health_command(claude_dir="/tmp/claude")
    arguments = desktop_app.build_local_health_arguments(claude_dir="/tmp/claude")

    assert "zerg.cli.main local-health --json" in command
    assert arguments[:4] == [arguments[0], "-m", "zerg.cli.main", "local-health"]
    assert arguments[-2:] == ["--claude-dir", "/tmp/claude"]


def test_default_install_desktop_app_respects_env(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_INSTALL_MENUBAR", "0")
    assert desktop_app.default_install_desktop_app() is False

    monkeypatch.setenv("LONGHOUSE_INSTALL_MENUBAR", "1")
    assert desktop_app.default_install_desktop_app() is True


def test_install_desktop_app_service_writes_plist_and_loads(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(
        desktop_app,
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

    monkeypatch.setattr(desktop_app.subprocess, "run", fake_run)

    result = desktop_app.install_desktop_app_service(
        ui_url="https://longhouse.ai",
        claude_dir=str(home / ".claude"),
    )

    plist_path = home / "Library" / "LaunchAgents" / "ai.longhouse.app.plist"
    assert plist_path.exists()
    plist = plist_path.read_text(encoding="utf-8")
    assert "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse" in plist
    assert "ai.longhouse.app" in plist
    assert "--health-exec" in plist
    assert "zerg.cli.main" in plist
    assert "https://longhouse.ai" in plist
    assert calls[-1] == ["launchctl", "load", str(plist_path)]
    assert result["plist_path"] == str(plist_path)
    assert result["app_path"] == "/Users/test/Applications/Longhouse.app"
    assert result["launch_path"] == "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse"


def test_get_desktop_app_service_status_returns_not_installed_when_plist_missing(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)

    assert desktop_app.get_desktop_app_service_status() == "not-installed"


def test_get_desktop_app_service_info_includes_app_bundle_details(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(desktop_app, "get_desktop_app_service_status", lambda: "running")
    monkeypatch.setattr(
        desktop_app,
        "resolve_installed_runtime_artifact",
        lambda component: SimpleNamespace(
            component=desktop_app.RuntimeComponent.DESKTOP_APP,
            path="/Users/test/Applications/Longhouse.app",
            launch_path="/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        ) if component == desktop_app.RuntimeComponent.DESKTOP_APP else None,
    )

    info = desktop_app.get_desktop_app_service_info()

    assert info["status"] == "running"
    assert info["service_name"] == "ai.longhouse.app"
    assert info["artifact_path"] == "/Users/test/Applications/Longhouse.app"
    assert info["launch_path"] == "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse"
    assert info["runtime_mode"] == "app-bundle"


def test_get_desktop_app_service_info_reads_legacy_plist_log_dir(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist_path = launch_agents / "com.longhouse.local-health-menubar.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.longhouse.local-health-menubar",
                "ProgramArguments": ["/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse"],
                "StandardOutPath": "/tmp/custom-claude/logs/local-health-menubar.stdout.log",
            }
        )
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(desktop_app, "get_desktop_app_service_status", lambda: "running")
    monkeypatch.setattr(desktop_app, "resolve_installed_runtime_artifact", lambda component: None)

    info = desktop_app.get_desktop_app_service_info()

    assert info["service_name"] == "com.longhouse.local-health-menubar"
    assert info["log_path"] == "/tmp/custom-claude/logs/local-health-menubar.*.log"
    assert info["runtime_mode"] == "broken-install"
