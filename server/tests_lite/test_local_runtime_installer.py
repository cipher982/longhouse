from types import SimpleNamespace

from zerg.services import local_runtime_installer as installer


def test_install_local_runtime_does_not_create_global_mcp_configs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    hook_calls: list[dict[str, str | None]] = []
    state_writes: list[dict[str, object]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "write_machine_state", lambda **kwargs: state_writes.append(kwargs))
    monkeypatch.setattr(installer, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(installer, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        installer,
        "ensure_runtime_binary",
        lambda component: SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
    )
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(
        installer,
        "install_hooks",
        lambda **kwargs: hook_calls.append(kwargs) or ["hooks installed"],
    )

    result = installer.install_local_runtime(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        machine_name="test-box",
        menubar=False,
    )

    assert result.machine_name == "test-box"
    assert result.hooks.actions == ["hooks installed"]
    assert result.hooks.warning is None
    assert state_writes == [
        {
            "base_dir": home / ".longhouse",
            "written_by": "connect-install",
            "runtime_url": "https://example.com",
            "machine_name": "test-box",
            "desktop_app_enabled": False,
            "topology_intent": None,
        }
    ]
    assert hook_calls == [
        {
            "url": "https://example.com",
            "token": None,
            "claude_dir": str(claude_dir),
            "engine_path": "/tmp/longhouse-engine",
        }
    ]
    assert not (home / ".claude.json").exists()
    assert not (home / ".codex" / "config.toml").exists()


def test_install_local_runtime_installs_desktop_app_when_requested(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    calls: list[tuple[str, dict[str, str | None]]] = []
    state_writes: list[dict[str, object]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "write_machine_state", lambda **kwargs: state_writes.append(kwargs))
    monkeypatch.setattr(installer, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(installer, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        installer,
        "ensure_runtime_binary",
        lambda component: SimpleNamespace(path="/tmp/longhouse-engine", installed_now=True),
    )
    monkeypatch.setattr(installer, "install_service", lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"})
    monkeypatch.setattr(installer, "install_hooks", lambda **kwargs: ["hooks installed"])
    monkeypatch.setattr(
        installer,
        "install_desktop_app_service",
        lambda **kwargs: calls.append(("desktop", kwargs)) or {
            "message": "desktop app installed",
            "plist_path": "/tmp/menubar.plist",
            "app_path": "/Applications/Longhouse.app",
            "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        },
    )

    result = installer.install_local_runtime(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        machine_name="test-box",
        menubar=True,
    )

    assert state_writes == [
        {
            "base_dir": home / ".longhouse",
            "written_by": "connect-install",
            "runtime_url": "https://example.com",
            "machine_name": "test-box",
            "desktop_app_enabled": True,
            "topology_intent": None,
        }
    ]
    assert calls == [
        (
            "desktop",
            {
                "ui_url": "https://example.com",
                "claude_dir": str(claude_dir),
            },
        )
    ]
    assert result.desktop_app_result == {
        "message": "desktop app installed",
        "plist_path": "/tmp/menubar.plist",
        "app_path": "/Applications/Longhouse.app",
        "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
    }


def test_install_local_runtime_keeps_service_install_when_hooks_warn(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    state_writes: list[dict[str, object]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "write_machine_state", lambda **kwargs: state_writes.append(kwargs))
    monkeypatch.setattr(installer, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(installer, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        installer,
        "ensure_runtime_binary",
        lambda component: SimpleNamespace(path="/tmp/longhouse-engine", installed_now=True),
    )
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(installer, "install_hooks", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("hooks boom")))

    result = installer.install_local_runtime(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        machine_name="test-box",
        menubar=False,
    )

    assert state_writes == [
        {
            "base_dir": home / ".longhouse",
            "written_by": "connect-install",
            "runtime_url": "https://example.com",
            "machine_name": "test-box",
            "desktop_app_enabled": False,
            "topology_intent": None,
        }
    ]
    assert result.service_result["message"] == "ok"
    assert result.hooks.actions == []
    assert result.hooks.warning == "hooks boom"
