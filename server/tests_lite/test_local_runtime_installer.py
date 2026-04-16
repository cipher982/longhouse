from types import SimpleNamespace

from zerg.services import local_runtime_installer as installer


def _stub_machine_state(**kwargs):
    return SimpleNamespace(
        schema_version=1,
        config_generation="test-generation",
        runtime_url=kwargs.get("runtime_url"),
        machine_name=kwargs.get("machine_name"),
        topology_intent=kwargs.get("topology_intent"),
        desktop_app_enabled=kwargs.get("desktop_app_enabled"),
        runner_enabled=kwargs.get("runner_enabled"),
        desired_bundle_version=kwargs.get("desired_bundle_version"),
    )


def test_install_local_runtime_does_not_create_global_mcp_configs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    hook_calls: list[dict[str, str | None]] = []
    state_writes: list[dict[str, object]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        installer,
        "write_machine_state",
        lambda **kwargs: state_writes.append(kwargs) or _stub_machine_state(**kwargs),
    )
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
    monkeypatch.setattr(
        installer,
        "write_machine_state",
        lambda **kwargs: state_writes.append(kwargs) or _stub_machine_state(**kwargs),
    )
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
        lambda **kwargs: {
            "message": "ok",
            "service": "launchd",
            "plist_path": "/tmp/test.plist",
        },
    )
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
    monkeypatch.setattr(
        installer,
        "write_machine_state",
        lambda **kwargs: state_writes.append(kwargs) or _stub_machine_state(**kwargs),
    )
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


def test_install_local_runtime_installs_managed_codex_when_configured(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    state_writes: list[dict[str, object]] = []
    ensure_calls: list[tuple[object, str | None]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        installer,
        "write_machine_state",
        lambda **kwargs: state_writes.append(kwargs) or _stub_machine_state(**kwargs),
    )
    monkeypatch.setattr(installer, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(installer, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        installer,
        "resolve_runtime_source_override",
        lambda component, *, source_override=None: source_override or ("/tmp/codex" if component.value == "managed-codex" else ""),
    )
    monkeypatch.setattr(installer, "resolve_installed_runtime_artifact", lambda component: None)

    def fake_ensure(component, *, source_override=None):
        ensure_calls.append((component, source_override))
        if component.value == "engine":
            return SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False)
        if component.value == "managed-codex":
            return SimpleNamespace(path="/tmp/longhouse-codex", installed_now=True)
        raise AssertionError(f"unexpected component: {component}")

    monkeypatch.setattr(installer, "ensure_runtime_binary", fake_ensure)
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(installer, "install_hooks", lambda **kwargs: ["hooks installed"])

    result = installer.install_local_runtime(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        machine_name="test-box",
        menubar=False,
        codex_source="/tmp/codex-patched",
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
    assert [(component.value, source_override) for component, source_override in ensure_calls] == [
        ("engine", None),
        ("managed-codex", "/tmp/codex-patched"),
    ]
    assert result.codex_runtime.path == "/tmp/longhouse-codex"
    assert result.codex_runtime.installed_now is True


def test_reconcile_local_runtime_uses_canonical_machine_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"

    monkeypatch.setenv("HOME", str(home))
    installer.write_machine_state(
        base_dir=home / ".longhouse",
        written_by="connect-install",
        runtime_url="https://example.com",
        machine_name="test-box",
        desktop_app_enabled=True,
        topology_intent="connect-remote",
    )

    service_calls: list[dict[str, str | None]] = []
    hook_calls: list[dict[str, str | None]] = []
    desktop_calls: list[dict[str, str | None]] = []

    monkeypatch.setattr(installer, "load_token", lambda config_dir: "stored-token")
    monkeypatch.setattr(
        installer,
        "ensure_runtime_binary",
        lambda component: SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
    )
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: service_calls.append(kwargs)
        or {
            "message": "ok",
            "service": "launchd",
            "plist_path": "/tmp/test.plist",
        },
    )
    monkeypatch.setattr(
        installer,
        "install_hooks",
        lambda **kwargs: hook_calls.append(kwargs) or ["hooks installed"],
    )
    monkeypatch.setattr(
        installer,
        "install_desktop_app_service",
        lambda **kwargs: desktop_calls.append(kwargs) or {
            "message": "desktop app installed",
            "plist_path": "/tmp/menubar.plist",
            "app_path": "/Applications/Longhouse.app",
            "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        },
    )

    result = installer.reconcile_local_runtime(
        claude_dir=str(claude_dir),
        written_by="machine-reconcile",
    )

    assert result.machine_state.runtime_url == "https://example.com"
    assert result.machine_state.machine_name == "test-box"
    assert result.machine_state.written_by == "machine-reconcile"
    assert result.install_result.machine_name == "test-box"
    assert len(service_calls) == 1
    assert service_calls[0]["url"] == "https://example.com"
    assert service_calls[0]["token"] == "stored-token"
    assert service_calls[0]["claude_dir"] == str(claude_dir)
    assert service_calls[0]["machine_name"] == "test-box"
    assert service_calls[0]["machine_config_generation"] == result.machine_state.config_generation
    assert service_calls[0]["machine_state_hash"] == installer.machine_state_source_hash(result.machine_state)
    assert hook_calls == [
        {
            "url": "https://example.com",
            "token": "stored-token",
            "claude_dir": str(claude_dir),
            "engine_path": "/tmp/longhouse-engine",
        }
    ]
    assert desktop_calls == [
        {
            "ui_url": "https://example.com",
            "claude_dir": str(claude_dir),
        }
    ]


def test_reconcile_local_runtime_requires_complete_machine_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"

    monkeypatch.setenv("HOME", str(home))
    installer.write_machine_state(
        base_dir=home / ".longhouse",
        written_by="connect-install",
        machine_name="test-box",
        desktop_app_enabled=True,
    )

    try:
        installer.reconcile_local_runtime(
            claude_dir=str(claude_dir),
            written_by="machine-reconcile",
        )
    except RuntimeError as exc:
        assert "missing runtime_url" in str(exc)
    else:
        raise AssertionError("expected reconcile_local_runtime to reject incomplete machine state")
