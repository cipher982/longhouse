"""Tests for the local install vs workspace MCP boundary."""

from pathlib import Path

from zerg.cli import connect


def test_handle_hooks_only_does_not_create_global_mcp_configs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(connect, "install_hooks", lambda url, token, claude_dir: ["hooks installed"])
    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)

    connect._handle_hooks_only("https://example.com", None, str(claude_dir))

    assert not (home / ".claude.json").exists()
    assert not (home / ".codex" / "config.toml").exists()


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


def test_connect_hooks_only_skips_auto_auth_when_no_token(monkeypatch):
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "_auto_create_token", lambda url: (_ for _ in ()).throw(AssertionError("should not auto-auth")))
    monkeypatch.setattr(
        connect,
        "_handle_hooks_only",
        lambda **kwargs: calls.append(("hooks", kwargs)),
    )

    connect.connect(
        url="https://example.com",
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

    assert calls == [
        (
            "hooks",
            {
                "url": "https://example.com",
                "token": None,
                "claude_dir": None,
            },
        )
    ]
