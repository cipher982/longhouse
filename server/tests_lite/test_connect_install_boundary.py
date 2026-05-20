"""Tests for the local install vs workspace MCP boundary."""

from types import SimpleNamespace

import pytest
from click.exceptions import Exit as ClickExit

from zerg.cli import connect
from zerg.services.machine_state import load_machine_state
from zerg.services.machine_state import write_machine_state
from zerg.services.shipper.service import Platform


def test_handle_install_delegates_to_shared_runtime_installer(monkeypatch, capsys):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)
    monkeypatch.setattr(
        connect,
        "reconcile_local_runtime",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            install_result=SimpleNamespace(
                machine_name="test-box",
                engine_runtime=SimpleNamespace(path="/tmp/longhouse-engine", installed_now=True),
                service_result={"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
                hooks=SimpleNamespace(actions=["hooks installed"], warning=None),
                desktop_app_result={
                    "message": "desktop app installed",
                    "plist_path": "/tmp/menubar.plist",
                    "app_path": "/Applications/Longhouse.app",
                    "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
                },
            )
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
            "token": None,
            "claude_dir": "/tmp/.claude",
            "written_by": "connect-install",
            "runtime_url": "https://example.com",
            "machine_name": "test-box",
            "menubar": True,
        }
    ]
    assert "Machine: test-box" in output
    assert "Engine binary installed at /tmp/longhouse-engine" in output
    assert "Longhouse.app:" in output
    assert "App: /Applications/Longhouse.app" in output


def test_handle_install_prompts_for_machine_name_when_missing(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)
    monkeypatch.setattr(connect.socket, "gethostname", lambda: "fallback-box")
    monkeypatch.setattr(connect.typer, "prompt", lambda message, default: "   ")
    monkeypatch.setattr(
        connect,
        "reconcile_local_runtime",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            install_result=SimpleNamespace(
                machine_name="fallback-box",
                engine_runtime=SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
                service_result={"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
                hooks=SimpleNamespace(actions=["hooks installed"], warning=None),
                desktop_app_result=None,
            )
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
            "token": None,
            "claude_dir": None,
            "written_by": "connect-install",
            "runtime_url": "https://example.com",
            "machine_name": "fallback-box",
            "menubar": False,
        }
    ]


def test_handle_install_reports_scratch_mode_skips(monkeypatch, capsys):
    monkeypatch.setattr(connect, "_verify_and_warn_path", lambda: None)
    monkeypatch.setattr(
        connect,
        "reconcile_local_runtime",
        lambda **kwargs: SimpleNamespace(
            install_result=SimpleNamespace(
                machine_name="test-box-dev",
                engine_runtime=SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
                service_result={
                    "message": "Scratch Longhouse home active; skipped global service install.",
                    "service": "skipped",
                },
                hooks=SimpleNamespace(
                    actions=["Scratch Longhouse home active; skipped Claude/Codex hook install."],
                    warning=None,
                ),
                desktop_app_result={
                    "message": "Scratch Longhouse home active; skipped desktop app install.",
                    "skipped": True,
                },
            )
        ),
    )

    connect._handle_install(
        url="http://127.0.0.1:8080",
        token=None,
        claude_dir=None,
        interval=1,
        machine_name="test-box-dev",
        menubar=True,
    )

    output = capsys.readouterr().out
    assert "[WARN] Scratch Longhouse home active; skipped global service install." in output
    assert "[WARN] Scratch Longhouse home active; skipped Claude/Codex hook install." in output
    assert "[WARN] Scratch Longhouse home active; skipped desktop app install." in output


def test_connect_install_skips_auto_auth_when_no_token(monkeypatch):
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: None)
    monkeypatch.setattr(
        connect,
        "_auto_create_token",
        lambda url: (_ for _ in ()).throw(AssertionError("should not auto-auth")),
    )
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


def test_connect_install_uses_stored_url_when_called_directly_without_url(monkeypatch):
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: "https://example.com")
    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: None)
    monkeypatch.setattr(
        connect,
        "_handle_install",
        lambda **kwargs: calls.append(("install", kwargs)),
    )

    connect.connect(
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
            "artifact_path": "/Applications/Longhouse.app",
            "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            "runtime_mode": "app-bundle",
        },
    )

    connect._handle_status()

    output = capsys.readouterr().out
    assert "Desktop App: ai.longhouse.app" in output
    assert "App: /Applications/Longhouse.app" in output
    assert "Launch: /Applications/Longhouse.app/Contents/MacOS/Longhouse" in output


def test_handle_status_accepts_local_source_build_desktop_app(monkeypatch, capsys):
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
            "artifact_path": "/Applications/Longhouse.app",
            "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            "runtime_mode": "source-build",
            "bundle_version": "0.0.0-dev",
        },
    )

    connect._handle_status()

    output = capsys.readouterr().out
    assert "Desktop App runtime: local source build (0.0.0-dev)" in output
    assert "install is missing, broken, or unsupported" not in output


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


def test_persist_selected_url_uses_safe_apply_path(monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        connect,
        "apply_machine_state_update",
        lambda **kwargs: calls.append(kwargs),
    )

    connect._persist_selected_url("https://example.com", str(claude_dir), written_by="connect")

    assert calls == [
        {
            "claude_dir": str(claude_dir),
            "written_by": "connect",
            "runtime_url": "https://example.com",
        }
    ]


def test_auth_clear_clears_canonical_runtime_url_without_touching_machine_name(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    write_machine_state(
        base_dir=claude_dir,
        written_by="test",
        runtime_url="https://example.com",
        machine_name="test-box",
    )
    cleared = connect.clear_machine_runtime_url(claude_dir, written_by="auth-clear")

    assert cleared is True
    state = load_machine_state(claude_dir)
    assert state is not None
    assert state.runtime_url is None
    assert state.machine_name == "test-box"
    assert state.written_by == "auth-clear"


def test_auth_direct_token_replays_spool_after_success(monkeypatch, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    longhouse_home = tmp_path / ".longhouse"
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "_validate_token", lambda url, token: True)
    monkeypatch.setattr(
        connect,
        "save_token",
        lambda token, config_dir: calls.append(("save", (token, config_dir))),
    )
    monkeypatch.setattr(
        connect,
        "_persist_selected_url",
        lambda url, claude_dir, written_by: calls.append(("persist", (url, claude_dir, written_by))),
    )
    monkeypatch.setattr(
        connect,
        "_attempt_post_auth_spool_replay",
        lambda **kwargs: calls.append(("replay", kwargs)),
    )

    connect.auth(
        url="https://example.com",
        device_name="cinder",
        token="zdt_direct",
        force=False,
        clear=False,
        claude_dir=str(claude_dir),
    )

    assert calls == [
        ("save", ("zdt_direct", longhouse_home)),
        ("persist", ("https://example.com", str(claude_dir), "auth")),
        ("replay", {"url": "https://example.com", "token": "zdt_direct", "claude_dir": str(claude_dir)}),
    ]


def test_auth_auto_token_replays_spool_after_success(monkeypatch):
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(connect, "load_token", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "get_zerg_url", lambda config_dir=None: None)
    monkeypatch.setattr(connect, "_auto_create_token", lambda url, device_name=None: "zdt_auto")
    monkeypatch.setattr(connect, "_validate_token", lambda url, token: True)
    monkeypatch.setattr(
        connect,
        "save_token",
        lambda token, config_dir: calls.append(("save", (token, config_dir))),
    )
    monkeypatch.setattr(
        connect,
        "_persist_selected_url",
        lambda url, claude_dir, written_by: calls.append(("persist", (url, claude_dir, written_by))),
    )
    monkeypatch.setattr(
        connect,
        "_attempt_post_auth_spool_replay",
        lambda **kwargs: calls.append(("replay", kwargs)),
    )

    connect.auth(
        url="https://example.com",
        device_name="cinder",
        token=None,
        force=False,
        clear=False,
        claude_dir=None,
    )

    assert calls == [
        ("save", ("zdt_auto", None)),
        ("persist", ("https://example.com", None, "auth")),
        ("replay", {"url": "https://example.com", "token": "zdt_auto", "claude_dir": None}),
    ]


def test_attempt_post_auth_spool_replay_preserves_auth_success_context(monkeypatch):
    messages: list[str] = []

    monkeypatch.setattr(
        connect,
        "replay_machine_backlog",
        lambda **kwargs: SimpleNamespace(
            warning="Queued shipping could not be replayed immediately. Run `longhouse ship` if backlog stays stuck."
        ),
    )
    monkeypatch.setattr(connect.typer, "secho", lambda message, fg=None: messages.append(message))

    connect._attempt_post_auth_spool_replay(
        url="https://example.com",
        token="zdt_auto",
        claude_dir=None,
    )

    assert messages == [
        "Authenticated, but queued shipping could not be replayed immediately. "
        "Run `longhouse ship` if backlog stays stuck."
    ]
