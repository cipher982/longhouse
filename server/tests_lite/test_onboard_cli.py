from __future__ import annotations

import os
import re
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import config_file as config_file_cli
from zerg.cli import onboard as onboard_cli
from zerg.cli.main import app


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def _install_result(*, hook_warning: str | None = None, desktop_app: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        machine_name="test-box",
        engine_runtime=SimpleNamespace(path="/tmp/longhouse-engine", installed_now=True),
        service_result={"message": "ok", "service": "launchd", "plist_path": "/tmp/test.plist"},
        hooks=SimpleNamespace(actions=["hooks installed"], warning=hook_warning),
        desktop_app_result={"message": "desktop app installed"} if desktop_app else None,
    )


def test_onboard_imports_existing_sessions_first(monkeypatch, tmp_path):
    runner = CliRunner()
    subprocess_calls: list[list[str]] = []
    install_calls: list[dict[str, object]] = []
    saved_configs: list[config_file_cli.LonghouseConfig] = []

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(
        onboard_cli,
        "save_loaded_config",
        lambda config, config_path=None: saved_configs.append(config),
    )
    monkeypatch.setattr(
        onboard_cli,
        "install_local_runtime",
        lambda **kwargs: install_calls.append(kwargs) or _install_result(),
    )

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(onboard_cli, "_runtime_host_command", lambda: ["longhouse-server"])

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "Install Longhouse, open it, and find one prior session." in result.output
    assert "Step 3: Bring in your existing sessions" in result.output
    assert "Importing your existing sessions now..." in result.output
    assert "[OK] Existing sessions are ready to look for in Longhouse" in result.output
    assert "Step 4: Saving configuration" in result.output
    assert "Step 5: PATH verification" in result.output
    assert "Find one prior session in the timeline" in result.output
    assert "longhouse claude" in result.output
    assert install_calls == [
        {
            "url": "http://127.0.0.1:8080",
            "token": None,
            "claude_dir": None,
            "machine_name": "test-box",
            "menubar": False,
            "written_by": "onboard",
        }
    ]
    assert len(saved_configs) == 1
    assert saved_configs[0].server.host == "127.0.0.1"
    assert saved_configs[0].server.port == 8080
    assert ["longhouse-server", "ship", "--url", "http://127.0.0.1:8080"] in subprocess_calls


def test_onboard_without_cli_skips_initial_import(monkeypatch, tmp_path):
    runner = CliRunner()
    subprocess_calls: list[list[str]] = []
    install_calls: list[dict[str, object]] = []

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda _cmd: False)
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)
    monkeypatch.setattr(
        onboard_cli,
        "install_local_runtime",
        lambda **kwargs: install_calls.append(kwargs) or _install_result(),
    )

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(onboard_cli, "_runtime_host_command", lambda: ["longhouse-server"])

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "No supported AI CLI found" in result.output
    assert "You can still set up the local runtime now and connect a CLI later." in result.output
    assert "[OK] Machine agent installed for automatic imports" in result.output
    assert "No supported CLI found yet, so Longhouse skipped the initial import." in result.output
    assert "Seeding demo sessions" not in result.output
    assert "longhouse-server serve --demo" in result.output
    assert ["longhouse-server", "ship", "--url", "http://127.0.0.1:8080"] not in subprocess_calls
    assert install_calls == [
        {
            "url": "http://127.0.0.1:8080",
            "token": None,
            "claude_dir": None,
            "machine_name": "test-box",
            "menubar": False,
            "written_by": "onboard",
        }
    ]


def test_onboard_does_not_suggest_an_excluded_native_entrypoint(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "agy")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)
    monkeypatch.setattr(onboard_cli, "install_local_runtime", lambda **_kwargs: _install_result())
    monkeypatch.setattr(
        onboard_cli.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    # Antigravity is still detected -- it imports into the timeline.
    assert "[OK] Antigravity found" in result.output
    # It is offered as a managed launch again as of 2026-08-20, because the
    # command it names now exists: engine/src/longhouse.rs declares an
    # `antigravity` subcommand and config/native_device_entrypoints.json is
    # `available`. Between 2026-07-31 and then this asserted the opposite, and
    # correctly so -- onboarding had been telling users to run a command that
    # did not exist. The invariant is that the two agree, not the value.
    assert "longhouse antigravity" in result.output
    # `longhouse agy` was never a subcommand and must not be suggested.
    assert "longhouse agy" not in result.output


def test_onboard_in_ci_skips_service_manager_install(monkeypatch, tmp_path):
    runner = CliRunner()
    subprocess_calls: list[list[str]] = []
    install_calls: list[dict[str, object]] = []

    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)
    monkeypatch.setattr(
        onboard_cli,
        "install_local_runtime",
        lambda **kwargs: install_calls.append(kwargs) or _install_result(),
    )

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(onboard_cli, "_runtime_host_command", lambda: ["longhouse-server"])

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "[--] Background machine-agent install is not available in this environment" in result.output
    assert "Use: longhouse machine repair --repair-service" in result.output
    assert ["longhouse-server", "ship", "--url", "http://127.0.0.1:8080"] in subprocess_calls
    assert install_calls == []


def test_onboard_topology_local_skips_prompt(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)
    monkeypatch.setattr(onboard_cli, "install_local_runtime", lambda **kwargs: _install_result())
    monkeypatch.setattr(
        onboard_cli.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )

    result = runner.invoke(app, ["onboard", "--topology", "local"])

    assert result.exit_code == 0, result.output
    assert "Where should your Longhouse server run?" not in result.output
    assert "Choose" not in result.output


def test_onboard_topology_remote_requires_url_in_noninteractive_mode(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(onboard_cli.sys.stdin, "isatty", lambda: False)

    result = runner.invoke(app, ["onboard", "--topology", "remote"])
    plain_output = _strip_ansi(result.output)

    assert result.exit_code != 0
    assert "Invalid value:" in plain_output
    assert "--topology remote requires --remote-url" in plain_output


def test_onboard_in_ci_can_install_services_when_explicitly_enabled(monkeypatch, tmp_path):
    runner = CliRunner()
    subprocess_calls: list[list[str]] = []
    open_calls: list[str] = []
    install_calls: list[dict[str, object]] = []
    app_path = tmp_path / "Applications" / "Longhouse.app"
    app_path.mkdir(parents=True)

    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("LONGHOUSE_INSTALL_SERVICES_IN_CI", "1")
    monkeypatch.setattr(onboard_cli.sys, "platform", "darwin")
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: True)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)
    monkeypatch.setattr(onboard_cli.webbrowser, "open", lambda url: open_calls.append(url) or True)
    monkeypatch.setattr(onboard_cli, "desktop_app_canonical_bundle_path", lambda: app_path)
    monkeypatch.setattr(
        onboard_cli,
        "install_local_runtime",
        lambda **kwargs: install_calls.append(kwargs) or _install_result(desktop_app=True),
    )

    def _fake_run(args: list[str], **kwargs):
        subprocess_calls.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(onboard_cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(onboard_cli, "_runtime_host_command", lambda: ["longhouse-server"])

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "[OK] Machine agent installed for automatic imports" in result.output
    assert "Look for Longhouse.app in /Applications and your menu bar" in result.output
    assert install_calls == [
        {
            "url": "http://127.0.0.1:8080",
            "token": None,
            "claude_dir": None,
            "machine_name": "test-box",
            "menubar": True,
            "written_by": "onboard",
        }
    ]
    assert ["open", str(app_path)] in subprocess_calls
    assert open_calls == []


def test_onboard_no_longer_prompts_for_manual_mode(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: False)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)

    result = runner.invoke(app, ["onboard", "--no-server", "--no-shipper"])

    assert result.exit_code == 0, result.output
    assert "Manual Setup" not in result.output
    assert "Choice" not in result.output
    assert "Step 5: PATH verification" in result.output
    assert "longhouse claude" in result.output


def test_onboard_suggests_cursor_and_opencode_managed_launch(monkeypatch, tmp_path):
    """Both were absent from onboarding entirely.

    A user whose only agent CLI was OpenCode or Cursor was told "No supported
    AI CLI found" and shown a three-item list that excluded what they had.
    """

    runner = CliRunner()

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd in {"cursor-agent", "opencode"})
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)
    monkeypatch.setattr(onboard_cli, "install_local_runtime", lambda **_kwargs: _install_result())
    monkeypatch.setattr(
        onboard_cli.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    assert "No supported AI CLI found" not in result.output
    assert "[OK] Cursor Agent found" in result.output
    assert "[OK] OpenCode found" in result.output
    assert "longhouse cursor" in result.output
    assert "longhouse opencode" in result.output


def test_onboard_never_advertises_a_verb_the_native_facade_dropped(monkeypatch, tmp_path):
    """Onboard's closing guidance must name commands that still exist.

    The Python device CLI was retired in favour of the native `longhouse`
    facade, which has no `doctor`, no `connect`, no `serve` and no `ship`.
    Onboard kept printing all four, so every repair hint it gave sent the user
    to an unrecognized subcommand.
    """
    runner = CliRunner()

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(onboard_cli, "_has_command", lambda cmd: cmd == "claude")
    monkeypatch.setattr(onboard_cli, "_has_launchd", lambda: True)
    monkeypatch.setattr(onboard_cli, "_has_systemd", lambda: False)
    monkeypatch.setattr(onboard_cli, "_is_server_running", lambda: (False, None))
    monkeypatch.setattr(onboard_cli, "_check_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(onboard_cli, "_has_gui", lambda: False)
    monkeypatch.setattr(onboard_cli.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr(onboard_cli, "load_token", lambda: None)
    monkeypatch.setattr(onboard_cli, "verify_shell_path", lambda: [])
    monkeypatch.setattr(onboard_cli, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(onboard_cli, "load_config", lambda config_path=None: config_file_cli.LonghouseConfig())
    monkeypatch.setattr(onboard_cli, "save_loaded_config", lambda config, config_path=None: None)
    monkeypatch.setattr(onboard_cli, "install_local_runtime", lambda **kwargs: _install_result())
    monkeypatch.setattr(
        onboard_cli.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0, result.output
    for retired in ("longhouse doctor", "longhouse connect", "longhouse ship", "longhouse serve", "longhouse status"):
        assert retired not in result.output, retired
    assert "longhouse machine repair" in result.output
    assert "longhouse-server ship" in result.output


def test_runtime_host_command_prefers_the_installed_console_script(monkeypatch):
    """`serve` and `ship` live on `longhouse-server`, never on the facade."""
    monkeypatch.setattr(onboard_cli.shutil, "which", lambda name: "/opt/bin/longhouse-server" if name == "longhouse-server" else None)

    assert onboard_cli._runtime_host_command() == ["/opt/bin/longhouse-server"]


def test_runtime_host_command_falls_back_to_the_running_interpreter(monkeypatch):
    monkeypatch.setattr(onboard_cli.shutil, "which", lambda name: None)

    assert onboard_cli._runtime_host_command() == [onboard_cli.sys.executable, "-m", "zerg.cli.main"]
