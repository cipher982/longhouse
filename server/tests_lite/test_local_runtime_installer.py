import plistlib
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

    def fake_ensure(component, *, source_override=None, overwrite=False):
        if component.value == "engine":
            return SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False)
        raise AssertionError(f"unexpected component: {component}")

    monkeypatch.setattr(installer, "ensure_runtime_binary", fake_ensure)
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


def test_install_local_runtime_removes_obsolete_managed_codex_runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    launcher = home / ".local" / "bin" / "longhouse-codex"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\n# longhouse-managed-codex-launcher\n")
    launcher.chmod(0o755)
    runtime_payload = home / ".longhouse" / "runtimes" / "codex" / "current" / "codex"
    runtime_payload.parent.mkdir(parents=True, exist_ok=True)
    runtime_payload.write_text("old codex")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        installer,
        "write_machine_state",
        lambda **kwargs: _stub_machine_state(**kwargs),
    )
    monkeypatch.setattr(installer, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(installer, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        installer,
        "ensure_runtime_binary",
        lambda component, **_kwargs: SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
    )
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(installer, "install_hooks", lambda **kwargs: ["hooks installed"])

    installer.install_local_runtime(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        machine_name="test-box",
        menubar=False,
    )

    assert not launcher.exists()
    assert not (home / ".longhouse" / "runtimes" / "codex").exists()


def test_obsolete_managed_codex_cleanup_preserves_unmarked_launcher(tmp_path, monkeypatch):
    home = tmp_path / "home"
    launcher = home / ".local" / "bin" / "longhouse-codex"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\necho user-owned\n")
    runtime_dir = home / ".longhouse" / "runtimes" / "codex"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))

    installer._cleanup_obsolete_managed_codex_runtime(home / ".longhouse")

    assert launcher.exists()
    assert launcher.read_text() == "#!/bin/sh\necho user-owned\n"
    assert not runtime_dir.exists()


def test_install_local_runtime_removes_obsolete_claude_managed_local_state_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    managed_local = claude_dir / "managed-local"
    for provider in ("codex-bridge", "opencode", "antigravity"):
        state_file = managed_local / provider / "nested" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{}")
    keep_dir = managed_local / "other-provider"
    keep_dir.mkdir(parents=True)
    (keep_dir / "state.json").write_text("{}")
    raw_transcript = claude_dir / "projects" / "demo" / "session.jsonl"
    raw_transcript.parent.mkdir(parents=True)
    raw_transcript.write_text('{"type":"user"}\n')

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        installer,
        "write_machine_state",
        lambda **kwargs: _stub_machine_state(**kwargs),
    )
    monkeypatch.setattr(installer, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(installer, "sanitize_machine_name", lambda machine_name: machine_name)
    monkeypatch.setattr(
        installer,
        "ensure_runtime_binary",
        lambda component, **_kwargs: SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
    )
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: {"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
    )
    monkeypatch.setattr(installer, "install_hooks", lambda **kwargs: ["hooks installed"])

    installer.install_local_runtime(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
        machine_name="test-box",
        menubar=False,
    )

    assert not (managed_local / "codex-bridge").exists()
    assert not (managed_local / "opencode").exists()
    assert not (managed_local / "antigravity").exists()
    assert (keep_dir / "state.json").read_text() == "{}"
    assert raw_transcript.read_text() == '{"type":"user"}\n'


def test_obsolete_claude_managed_local_cleanup_skips_global_claude_when_scratch_home_has_no_provider_dir(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    global_state = home / ".claude" / "managed-local" / "codex-bridge" / "state.json"
    global_state.parent.mkdir(parents=True)
    global_state.write_text("{}")

    monkeypatch.setenv("HOME", str(home))

    installer._cleanup_obsolete_claude_managed_local_state(
        config_dir=home / ".longhouse-dev",
        claude_dir=None,
    )

    assert global_state.read_text() == "{}"


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

    def fake_ensure(component, *, source_override=None, overwrite=False):
        if component.value == "engine":
            return SimpleNamespace(path="/tmp/longhouse-engine", installed_now=True)
        raise AssertionError(f"unexpected component: {component}")

    monkeypatch.setattr(installer, "ensure_runtime_binary", fake_ensure)
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
        lambda **kwargs: calls.append(("desktop", kwargs))
        or {
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

    def fake_ensure(component, *, source_override=None, overwrite=False):
        if component.value == "engine":
            return SimpleNamespace(path="/tmp/longhouse-engine", installed_now=True)
        raise AssertionError(f"unexpected component: {component}")

    monkeypatch.setattr(installer, "ensure_runtime_binary", fake_ensure)
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


def test_install_local_runtime_skips_global_integrations_for_scratch_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    scratch_home = home / ".longhouse-dev"
    state_writes: list[dict[str, object]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LONGHOUSE_HOME", str(scratch_home))
    monkeypatch.setattr(
        installer,
        "write_machine_state",
        lambda **kwargs: state_writes.append(kwargs) or _stub_machine_state(**kwargs),
    )
    monkeypatch.setattr(installer, "save_token", lambda token, config_dir: None)
    monkeypatch.setattr(installer, "sanitize_machine_name", lambda machine_name: machine_name)

    def fake_ensure(component, *, source_override=None, overwrite=False):
        if component.value == "engine":
            return SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False)
        raise AssertionError(f"unexpected component: {component}")

    monkeypatch.setattr(installer, "ensure_runtime_binary", fake_ensure)
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch install should skip service install")),
    )
    monkeypatch.setattr(
        installer,
        "install_hooks",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch install should skip hook install")),
    )
    monkeypatch.setattr(
        installer,
        "install_desktop_app_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch install should skip desktop app install")),
    )

    result = installer.install_local_runtime(
        url="http://127.0.0.1:8080",
        token=None,
        claude_dir=None,
        machine_name="test-box-dev",
        menubar=True,
    )

    assert state_writes == [
        {
            "base_dir": scratch_home,
            "written_by": "connect-install",
            "runtime_url": "http://127.0.0.1:8080",
            "machine_name": "test-box-dev",
            "desktop_app_enabled": True,
            "topology_intent": None,
        }
    ]
    assert result.service_result["service"] == "skipped"
    assert "Scratch Longhouse home active" in result.service_result["message"]
    assert result.hooks.actions == ["Scratch Longhouse home active; skipped Claude/Codex hook install."]
    assert result.desktop_app_result == {
        "message": "Scratch Longhouse home active; skipped desktop app install.",
        "skipped": True,
    }


def test_apply_machine_state_update_persists_without_reconciling_when_service_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "get_service_info", lambda claude_dir: {"status": "not-installed"})

    result = installer.apply_machine_state_update(
        claude_dir=str(claude_dir),
        written_by="connect",
        runtime_url="https://example.com",
    )

    assert result.reconciled is False
    assert result.machine_state.runtime_url == "https://example.com"
    _state_path, loaded, error = installer.read_machine_state(home / ".longhouse")
    assert error is None
    assert loaded is not None
    assert loaded.runtime_url == "https://example.com"
    assert loaded.written_by == "connect"


def test_apply_machine_state_update_rejects_localhost_target_on_stable_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "get_service_info", lambda claude_dir: {"status": "not-installed"})
    monkeypatch.setattr(
        installer,
        "collect_launch_readiness",
        lambda *args, **kwargs: {
            "reasons": ["config_url_runner_url_mismatch"],
            "runner": {
                "runner_name": "cinder",
                "runner_urls": ["https://demo.longhouse.test"],
            },
        },
    )

    try:
        installer.apply_machine_state_update(
            claude_dir=str(claude_dir),
            written_by="connect",
            runtime_url="http://127.0.0.1:8080",
            machine_name="cinder",
        )
    except RuntimeError as exc:
        assert "Refusing to point the stable Longhouse home at a local control plane" in str(exc)
        assert "LONGHOUSE_HOME=~/.longhouse-dev" in str(exc)
    else:
        raise AssertionError("expected apply_machine_state_update to reject localhost on the stable home")


def test_apply_machine_state_update_allows_localhost_target_on_scratch_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    scratch_home = home / ".longhouse-dev"
    collect_calls: list[dict[str, object]] = []

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "get_service_info", lambda claude_dir: {"status": "not-installed"})
    monkeypatch.setattr(
        installer,
        "collect_launch_readiness",
        lambda *args, **kwargs: collect_calls.append(kwargs) or {"reasons": ["config_url_runner_url_mismatch"]},
    )

    result = installer.apply_machine_state_update(
        claude_dir=None,
        base_dir=scratch_home,
        written_by="connect",
        runtime_url="http://127.0.0.1:8080",
        machine_name="cinder-dev",
    )

    assert result.reconciled is False
    assert result.machine_state.runtime_url == "http://127.0.0.1:8080"
    assert collect_calls == []


def test_apply_machine_state_update_skips_global_reconcile_for_scratch_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    scratch_home = home / ".longhouse-dev"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(installer, "get_service_info", lambda claude_dir: {"status": "running"})
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch apply should skip service reconcile")),
    )
    monkeypatch.setattr(
        installer,
        "install_hooks",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch apply should skip hook reconcile")),
    )
    monkeypatch.setattr(
        installer,
        "install_desktop_app_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch apply should skip desktop app reconcile")),
    )

    result = installer.apply_machine_state_update(
        claude_dir=None,
        base_dir=scratch_home,
        written_by="shipper-save-url",
        runtime_url="https://dev.longhouse.test",
    )

    assert result.reconciled is False
    assert result.machine_state.runtime_url == "https://dev.longhouse.test"


def test_apply_machine_state_update_reconciles_existing_service(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"

    monkeypatch.setenv("HOME", str(home))
    installer.write_machine_state(
        base_dir=home / ".longhouse",
        written_by="connect-install",
        runtime_url="https://old.longhouse.test",
        machine_name="test-box",
        desktop_app_enabled=True,
        topology_intent="connect-remote",
    )

    service_calls: list[dict[str, str | None]] = []
    hook_calls: list[dict[str, str | None]] = []
    desktop_calls: list[dict[str, str | None]] = []

    monkeypatch.setattr(installer, "get_service_info", lambda claude_dir: {"status": "running"})
    monkeypatch.setattr(installer, "load_token", lambda config_dir: "stored-token")
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
        lambda **kwargs: desktop_calls.append(kwargs)
        or {
            "message": "desktop app installed",
            "plist_path": "/tmp/menubar.plist",
            "app_path": "/Applications/Longhouse.app",
            "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        },
    )

    result = installer.apply_machine_state_update(
        claude_dir=str(claude_dir),
        written_by="connect",
        runtime_url="https://new.longhouse.test",
    )

    assert result.reconciled is True
    assert result.machine_state.runtime_url == "https://new.longhouse.test"
    assert result.machine_state.machine_name == "test-box"
    assert service_calls == [
        {
            "url": "https://new.longhouse.test",
            "token": "stored-token",
            "claude_dir": str(claude_dir),
            "machine_name": "test-box",
            "machine_config_generation": result.machine_state.config_generation,
            "machine_state_hash": installer.machine_state_source_hash(result.machine_state),
        }
    ]
    assert hook_calls == [
        {
            "url": "https://new.longhouse.test",
            "token": "stored-token",
            "claude_dir": str(claude_dir),
        }
    ]
    assert desktop_calls == [
        {
            "ui_url": "https://new.longhouse.test",
            "claude_dir": str(claude_dir),
        }
    ]


def test_apply_machine_state_update_ignores_service_from_other_state_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    other_home = tmp_path / "other-home"
    other_home.mkdir(parents=True, exist_ok=True)
    service_file = tmp_path / "com.longhouse.shipper.plist"
    service_file.write_bytes(
        b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>EnvironmentVariables</key><dict><key>LONGHOUSE_HOME</key><string>"""
        + str(other_home).encode("utf-8")
        + b"""</string></dict></dict></plist>"""
    )

    monkeypatch.setenv("HOME", str(home))
    installer.write_machine_state(
        base_dir=home / ".longhouse",
        written_by="connect-install",
        runtime_url="https://old.longhouse.test",
        machine_name="test-box",
        desktop_app_enabled=True,
    )
    monkeypatch.setattr(
        installer,
        "get_service_info",
        lambda claude_dir: {"status": "running", "service_file": str(service_file)},
    )

    result = installer.apply_machine_state_update(
        claude_dir=str(claude_dir),
        written_by="connect",
        runtime_url="https://new.longhouse.test",
    )

    assert result.reconciled is False
    assert result.machine_state.runtime_url == "https://new.longhouse.test"


def test_apply_machine_state_update_reconciles_symlinked_state_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    real_home = tmp_path / "real-longhouse"
    real_home.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    (home / ".longhouse").symlink_to(real_home, target_is_directory=True)
    service_file = tmp_path / "com.longhouse.shipper.plist"
    service_file.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "LONGHOUSE_HOME": str(real_home),
                }
            }
        )
    )

    monkeypatch.setenv("HOME", str(home))
    installer.write_machine_state(
        base_dir=home / ".longhouse",
        written_by="connect-install",
        runtime_url="https://old.longhouse.test",
        machine_name="test-box",
        desktop_app_enabled=False,
    )

    service_calls: list[dict[str, str | None]] = []
    monkeypatch.setattr(
        installer,
        "get_service_info",
        lambda claude_dir: {"status": "running", "service_file": str(service_file)},
    )
    monkeypatch.setattr(installer, "load_token", lambda config_dir: "stored-token")
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
    monkeypatch.setattr(installer, "install_hooks", lambda **kwargs: ["hooks installed"])

    result = installer.apply_machine_state_update(
        claude_dir=str(claude_dir),
        written_by="connect",
        runtime_url="https://new.longhouse.test",
    )

    assert result.reconciled is True
    assert service_calls == [
        {
            "url": "https://new.longhouse.test",
            "token": "stored-token",
            "claude_dir": str(claude_dir),
            "machine_name": "test-box",
            "machine_config_generation": result.machine_state.config_generation,
            "machine_state_hash": installer.machine_state_source_hash(result.machine_state),
        }
    ]


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

    def fake_ensure(component, *, source_override=None, overwrite=False):
        if component.value == "engine":
            return SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False)
        raise AssertionError(f"unexpected component: {component}")

    monkeypatch.setattr(installer, "ensure_runtime_binary", fake_ensure)
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
        lambda **kwargs: desktop_calls.append(kwargs)
        or {
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


def test_reconcile_local_runtime_skips_global_integrations_for_scratch_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    scratch_home = home / ".longhouse-dev"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LONGHOUSE_HOME", str(scratch_home))
    installer.write_machine_state(
        base_dir=scratch_home,
        written_by="connect-install",
        runtime_url="http://127.0.0.1:8080",
        machine_name="test-box-dev",
        desktop_app_enabled=True,
    )
    monkeypatch.setattr(installer, "load_token", lambda config_dir: "stored-token")

    def fake_ensure(component, *, source_override=None, overwrite=False):
        if component.value == "engine":
            return SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False)
        raise AssertionError(f"unexpected component: {component}")

    monkeypatch.setattr(installer, "ensure_runtime_binary", fake_ensure)
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch reconcile should skip service install")),
    )
    monkeypatch.setattr(
        installer,
        "install_hooks",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch reconcile should skip hook install")),
    )
    monkeypatch.setattr(
        installer,
        "install_desktop_app_service",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("scratch reconcile should skip desktop app install")),
    )

    result = installer.reconcile_local_runtime(
        claude_dir=None,
        written_by="machine-reconcile",
    )

    assert result.machine_state.runtime_url == "http://127.0.0.1:8080"
    assert result.machine_state.machine_name == "test-box-dev"
    assert result.install_result.service_result["service"] == "skipped"
    assert result.install_result.hooks.actions == ["Scratch Longhouse home active; skipped Claude/Codex hook install."]
    assert result.install_result.desktop_app_result == {
        "message": "Scratch Longhouse home active; skipped desktop app install.",
        "skipped": True,
    }
