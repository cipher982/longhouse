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
        "install_service",
        lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(connect, "install_hooks", lambda url, token, claude_dir: ["hooks installed"])
    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)

    connect._handle_install(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        poll=False,
        interval=1,
        machine_name="test-box",
    )

    assert not (home / ".claude.json").exists()
    assert not (home / ".codex" / "config.toml").exists()
