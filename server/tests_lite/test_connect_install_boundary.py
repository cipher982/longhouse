"""Tests for the local install vs workspace MCP boundary."""

from pathlib import Path

import pytest
from click.exceptions import Exit as ClickExit

from zerg.cli import connect
from zerg.services.shipper.service import Platform


def test_handle_install_does_not_create_global_mcp_configs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(connect, "save_zerg_url", lambda url, config_dir: None)
    monkeypatch.setattr(connect, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(connect, "save_machine_name", lambda machine_name, config_dir: None)
    monkeypatch.setattr(connect, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        connect,
        "ensure_runtime_binary",
        lambda component: type("Result", (), {"path": "/tmp/longhouse-engine", "installed_now": False})(),
    )
    monkeypatch.setattr(
        connect,
        "install_service",
        lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(connect, "install_hooks", lambda url, token, claude_dir: ["hooks installed"])
    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)

    connect._handle_install(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        interval=1,
        machine_name="test-box",
    )

    assert not (home / ".claude.json").exists()
    assert not (home / ".codex" / "config.toml").exists()


def test_handle_install_installs_menubar_when_requested(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    calls: list[tuple[str, dict]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(connect, "save_zerg_url", lambda url, config_dir: None)
    monkeypatch.setattr(connect, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(connect, "save_machine_name", lambda machine_name, config_dir: None)
    monkeypatch.setattr(connect, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        connect,
        "ensure_runtime_binary",
        lambda component: type("Result", (), {"path": "/tmp/longhouse-engine", "installed_now": True})(),
    )
    monkeypatch.setattr(connect, "install_service", lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"})
    monkeypatch.setattr(connect, "install_hooks", lambda url, token, claude_dir: ["hooks installed"])
    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)
    monkeypatch.setattr(
        connect,
        "install_menubar_service",
        lambda **kwargs: calls.append(("menubar", kwargs)) or {
            "message": "ambient installed",
            "plist_path": "/tmp/menubar.plist",
            "binary_path": "/tmp/menubar-bin",
        },
    )

    connect._handle_install(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        interval=1,
        machine_name="test-box",
        menubar=True,
    )

    assert calls == [
        (
            "menubar",
            {
                "ui_url": "https://example.com",
                "claude_dir": str(claude_dir),
            },
        )
    ]


def test_connect_install_skips_auto_auth_when_no_token(monkeypatch):
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "_auto_create_token", lambda url: (_ for _ in ()).throw(AssertionError("should not auto-auth")))
    monkeypatch.setattr(
        connect,
        "_handle_install",
        lambda **kwargs: calls.append(("install", kwargs)),
    )

    connect.connect(
        url="https://example.com",
        token=None,
        interval=300,
        debounce=500,
        claude_dir=None,
        verbose=False,
        install=True,
        hooks_only=False,
        uninstall=False,
        status=False,
        machine_name="test-box",
        menubar=False,
    )

    assert calls == [
        (
            "install",
            {
                "url": "https://example.com",
                "token": None,
                "claude_dir": None,
                "interval": 300,
                "machine_name": "test-box",
                "menubar": False,
            },
        )
    ]


def test_handle_status_shows_ambient_app_bundle_details(monkeypatch, capsys):
    monkeypatch.setattr(
        connect,
        "get_service_info",
        lambda: {
            "platform": "macos",
            "status": "running",
            "service_name": "com.longhouse.shipper",
            "service_file": "/tmp/shipper.plist",
            "log_path": "/tmp/engine.log",
        },
    )
    monkeypatch.setattr(connect, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(
        connect,
        "get_menubar_service_info",
        lambda: {
            "status": "running",
            "service_name": "com.longhouse.local-health-menubar",
            "service_file": "/tmp/menubar.plist",
            "log_path": "/tmp/menubar.log",
            "artifact_path": "/Users/test/Applications/Longhouse.app",
            "launch_path": "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            "runtime_mode": "app-bundle",
        },
    )

    connect._handle_status()

    output = capsys.readouterr().out
    assert "Desktop App: com.longhouse.local-health-menubar" in output
    assert "App: /Users/test/Applications/Longhouse.app" in output
    assert "Launch: /Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse" in output


def test_connect_hooks_only_exits_with_error(monkeypatch):
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: "https://example.com")
    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: None)

    with pytest.raises(ClickExit) as exc:
        connect.connect(
            url=None,
            token=None,
            interval=300,
            debounce=500,
            claude_dir=None,
            verbose=False,
            install=False,
            hooks_only=True,
            uninstall=False,
            status=False,
            machine_name=None,
            menubar=False,
        )
    assert exc.value.exit_code == 1


def test_ship_requires_configured_url(monkeypatch):
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)

    with pytest.raises(ClickExit) as exc:
        connect.ship(url=None, token=None, file=None, claude_dir=None, verbose=False, quiet=False)
    assert exc.value.exit_code == 1


def test_connect_requires_configured_url(monkeypatch):
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)

    with pytest.raises(ClickExit) as exc:
        connect.connect(
            url=None,
            token=None,
            interval=300,
            debounce=500,
            claude_dir=None,
            verbose=False,
            install=True,
            hooks_only=False,
            uninstall=False,
            status=False,
            machine_name="test-box",
            menubar=False,
        )
    assert exc.value.exit_code == 1
