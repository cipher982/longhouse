"""Tests for the local install vs workspace MCP boundary."""

from types import SimpleNamespace

import pytest
from click.exceptions import Exit as ClickExit

from zerg.cli import connect
from zerg.services.shipper.service import Platform


def test_handle_install_delegates_to_shared_runtime_installer(monkeypatch, capsys):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)
    monkeypatch.setattr(
        connect,
        "install_local_runtime",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            machine_name="test-box",
            engine_runtime=SimpleNamespace(path="/tmp/longhouse-engine", installed_now=True),
            service_result={"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
            hooks=SimpleNamespace(actions=["hooks installed"], warning=None),
            desktop_app_result={
                "message": "desktop app installed",
                "plist_path": "/tmp/menubar.plist",
                "app_path": "/Users/test/Applications/Longhouse.app",
                "launch_path": "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            },
        ),
    )

    connect._handle_install(
        url="https://example.com",
        token=None,
        claude_dir="/tmp/.claude",
        interval=1,
        machine_name="test-box",
        menubar=True,
    )

    output = capsys.readouterr().out
    assert calls == [
        {
            "url": "https://example.com",
            "token": None,
            "claude_dir": "/tmp/.claude",
            "machine_name": "test-box",
            "menubar": True,
        }
    ]
    assert "Machine: test-box" in output
    assert "Engine binary installed at /tmp/longhouse-engine" in output
    assert "Longhouse.app:" in output
    assert "App: /Users/test/Applications/Longhouse.app" in output


def test_handle_install_prompts_for_machine_name_when_missing(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)
    monkeypatch.setattr(connect.socket, "gethostname", lambda: "fallback-box")
    monkeypatch.setattr(connect.typer, "prompt", lambda message, default: "   ")
    monkeypatch.setattr(
        connect,
        "install_local_runtime",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            machine_name="fallback-box",
            engine_runtime=SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
            service_result={"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
            hooks=SimpleNamespace(actions=["hooks installed"], warning=None),
            desktop_app_result=None,
        ),
    )

    connect._handle_install(
        url="https://example.com",
        token=None,
        claude_dir=None,
        interval=1,
        machine_name=None,
        menubar=False,
    )

    assert calls == [
        {
            "url": "https://example.com",
            "token": None,
            "claude_dir": None,
            "machine_name": "fallback-box",
            "menubar": False,
        }
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
        "get_desktop_app_service_info",
        lambda: {
            "status": "running",
            "service_name": "ai.longhouse.app",
            "service_file": "/tmp/menubar.plist",
            "log_path": "/tmp/menubar.log",
            "artifact_path": "/Users/test/Applications/Longhouse.app",
            "launch_path": "/Users/test/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            "runtime_mode": "app-bundle",
        },
    )

    connect._handle_status()

    output = capsys.readouterr().out
    assert "Desktop App: ai.longhouse.app" in output
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
