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


def _write_app_bundle(app_bundle: Path, *, version: str) -> None:
    contents = app_bundle / "Contents"
    macos_dir = contents / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    (macos_dir / "Longhouse").write_text("#!/bin/sh\n", encoding="utf-8")
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleShortVersionString": version,
                "CFBundleVersion": version,
            }
        )
    )


def test_build_snapshot_command_prefers_dedicated_local_health_script(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    health_path = home / ".local" / "bin" / "longhouse-local-health"
    health_path.parent.mkdir(parents=True)
    health_path.write_text("#!/bin/sh\n", encoding="utf-8")
    health_path.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app.shutil, "which", lambda _name: None)

    command = desktop_app.build_snapshot_command(claude_dir="/tmp/claude")
    arguments = desktop_app.build_snapshot_arguments(claude_dir="/tmp/claude")

    assert command.startswith(str(health_path))
    assert arguments[:3] == [str(health_path), "--fast", "--json"]
    assert arguments[-2:] == ["--claude-dir", "/tmp/claude"]


def test_build_snapshot_command_falls_back_to_stable_user_local_cli(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    cli_path = home / ".local" / "bin" / "longhouse"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("#!/bin/sh\n", encoding="utf-8")
    cli_path.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app.shutil, "which", lambda _name: None)

    arguments = desktop_app.build_snapshot_arguments(claude_dir="/tmp/claude")

    assert arguments[:4] == [str(cli_path), "local-health", "--fast", "--json"]
    assert arguments[-2:] == ["--claude-dir", "/tmp/claude"]


def test_build_snapshot_command_falls_back_to_fast_module_when_cli_missing(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app.shutil, "which", lambda _name: None)

    arguments = desktop_app.build_snapshot_arguments()

    assert arguments[:4] == [arguments[0], "-m", "zerg.cli.local_health_fast", "--fast"]


def test_default_install_desktop_app_respects_env(monkeypatch):
    monkeypatch.setenv("LONGHOUSE_INSTALL_MENUBAR", "0")
    assert desktop_app.default_install_desktop_app() is False

    monkeypatch.setenv("LONGHOUSE_INSTALL_MENUBAR", "1")
    assert desktop_app.default_install_desktop_app() is True


def test_install_desktop_app_service_writes_plist_and_loads(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app.shutil, "which", lambda _name: None)
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(
        desktop_app,
        "ensure_runtime_artifact",
        lambda component, source_override=None: SimpleNamespace(
            path="/Applications/Longhouse.app",
            launch_path="/Applications/Longhouse.app/Contents/MacOS/Longhouse",
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
    assert "/Applications/Longhouse.app/Contents/MacOS/Longhouse" in plist
    assert "ai.longhouse.app" in plist
    assert "--health-exec" in plist
    assert "zerg.cli.local_health_fast" in plist
    assert "<string>30</string>" in plist
    assert "https://longhouse.ai" in plist
    assert str(home / ".longhouse" / "agent" / "logs" / "desktop-app.stdout.log") in plist
    assert calls[-1] == ["launchctl", "load", str(plist_path)]
    assert result["plist_path"] == str(plist_path)
    assert result["app_path"] == "/Applications/Longhouse.app"
    assert result["launch_path"] == "/Applications/Longhouse.app/Contents/MacOS/Longhouse"


def test_install_desktop_app_service_omits_invalid_ui_url(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(
        desktop_app,
        "ensure_runtime_artifact",
        lambda component, source_override=None: SimpleNamespace(
            path="/Applications/Longhouse.app",
            launch_path="/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            source="override",
            installed_now=True,
        ),
    )
    monkeypatch.setattr(
        desktop_app.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )

    desktop_app.install_desktop_app_service(
        ui_url="https://<typer.models.OptionInfo object at 0x1234>",
        claude_dir=str(home / ".claude"),
    )

    plist_path = home / "Library" / "LaunchAgents" / "ai.longhouse.app.plist"
    plist = plist_path.read_text(encoding="utf-8")
    assert "--ui-url" not in plist


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
            path="/Applications/Longhouse.app",
            launch_path="/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        ) if component == desktop_app.RuntimeComponent.DESKTOP_APP else None,
    )

    info = desktop_app.get_desktop_app_service_info()

    assert info["status"] == "running"
    assert info["service_name"] == "ai.longhouse.app"
    assert info["artifact_path"] == "/Applications/Longhouse.app"
    assert info["launch_path"] == "/Applications/Longhouse.app/Contents/MacOS/Longhouse"
    assert info["runtime_mode"] == "app-bundle"


def test_get_desktop_app_service_info_flags_missing_health_exec_even_with_app_bundle(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    missing_health_exec = tmp_path / "deleted-worktree" / "server" / ".venv" / "bin" / "python"
    plist_path = launch_agents / "ai.longhouse.app.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.longhouse.app",
                "ProgramArguments": [
                    "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
                    "--live",
                    "--health-exec",
                    str(missing_health_exec),
                    "--health-arg",
                    "-m",
                    "--health-arg",
                    "zerg.cli.main",
                ],
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(desktop_app, "get_desktop_app_service_status", lambda: "running")
    monkeypatch.setattr(
        desktop_app,
        "resolve_installed_runtime_artifact",
        lambda component: SimpleNamespace(
            component=desktop_app.RuntimeComponent.DESKTOP_APP,
            path="/Applications/Longhouse.app",
            launch_path="/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        ) if component == desktop_app.RuntimeComponent.DESKTOP_APP else None,
    )

    info = desktop_app.get_desktop_app_service_info()

    assert info["artifact_path"] == "/Applications/Longhouse.app"
    assert info["runtime_mode"] == "broken-health-exec"
    assert info["health_exec_path"] == str(missing_health_exec)
    assert info["health_exec_exists"] == "false"
    assert info["health_exec_error"] == "configured health executable is missing"


def test_get_desktop_app_service_info_reads_legacy_plist_log_dir(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    legacy_app = tmp_path / "Legacy" / "Longhouse.app"
    plist_path = launch_agents / "com.longhouse.local-health-menubar.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.longhouse.local-health-menubar",
                "ProgramArguments": [str(legacy_app / "Contents" / "MacOS" / "Longhouse")],
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


def test_get_desktop_app_service_info_marks_local_source_build(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    app_bundle = tmp_path / "Applications" / "Longhouse.app"
    _write_app_bundle(app_bundle, version="0.0.0-dev")
    plist_path = launch_agents / "ai.longhouse.app.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.longhouse.app",
                "ProgramArguments": [str(app_bundle / "Contents" / "MacOS" / "Longhouse")],
            }
        )
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(desktop_app, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(desktop_app, "get_desktop_app_service_status", lambda: "running")
    monkeypatch.setattr(desktop_app, "resolve_installed_runtime_artifact", lambda component: None)
    monkeypatch.setattr(desktop_app, "desktop_app_canonical_bundle_path", lambda: app_bundle)

    info = desktop_app.get_desktop_app_service_info()

    assert info["service_name"] == "ai.longhouse.app"
    assert info["runtime_mode"] == "source-build"
    assert info["bundle_version"] == "0.0.0-dev"
