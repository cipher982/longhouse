from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli.main import app
from zerg.cli import update_manager


def test_write_install_metadata_persists_file(monkeypatch, tmp_path):
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    monkeypatch.setenv("HOME", str(runner_home))
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")

    metadata_payload = update_manager.write_install_metadata(
        install_method="uv",
        install_source="pypi",
        package_name="longhouse",
        channel="stable",
    )

    install_json = runner_home / ".longhouse" / "install.json"
    assert install_json.exists()
    assert metadata_payload.install_method == "uv"
    assert metadata_payload.install_source == "pypi"
    assert metadata_payload.installed_version == "0.1.5"


def test_version_command_reports_installed_version(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.1.5"


def test_version_command_check_reports_update(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(
        update_manager,
        "check_for_updates",
        lambda package_name="longhouse": update_manager.UpdateCheckResult(
            installed_version="0.1.5",
            latest_version="0.1.6",
            update_available=True,
            install_method="uv",
            install_source="pypi",
            upgrade_command="uv tool upgrade longhouse",
            package_name="longhouse",
        ),
    )

    result = runner.invoke(app, ["version", "--check"])

    assert result.exit_code == 0, result.output
    assert "Installed: 0.1.5" in result.output
    assert "Latest:    0.1.6" in result.output
    assert "Update available." in result.output
    assert "uv tool upgrade longhouse" in result.output


def test_version_command_check_returns_json_error_on_failure(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")
    monkeypatch.setattr(update_manager, "check_for_updates", lambda package_name="longhouse": (_ for _ in ()).throw(RuntimeError("boom")))

    result = runner.invoke(app, ["version", "--check", "--json"])

    assert result.exit_code == 1, result.output
    assert '"installed_version": "0.1.5"' in result.output
    assert '"error": "boom"' in result.output


def test_upgrade_command_runs_uv_tool_upgrade_and_records_metadata(monkeypatch, tmp_path):
    runner = CliRunner()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    monkeypatch.setenv("HOME", str(runner_home))
    monkeypatch.setattr(
        update_manager,
        "detect_install_metadata",
        lambda: update_manager.InstallMetadata(
            install_method="uv",
            install_source="pypi",
            package_name="longhouse",
            channel="stable",
            installed_version="0.1.5",
            installed_at="2026-04-07T00:00:00+00:00",
            last_upgrade_at="2026-04-07T00:00:00+00:00",
        ),
    )
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.6")

    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_manager.subprocess, "run", fake_run)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "upgrade", "longhouse"]]
    metadata_payload = update_manager.load_install_metadata()
    assert metadata_payload is not None
    assert metadata_payload.install_source == "pypi"
    assert metadata_payload.installed_version == "0.1.6"


def test_upgrade_command_override_source_reinstalls_from_custom_package(monkeypatch, tmp_path):
    runner = CliRunner()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    monkeypatch.setenv("HOME", str(runner_home))
    monkeypatch.setattr(
        update_manager,
        "detect_install_metadata",
        lambda: update_manager.InstallMetadata(
            install_method="uv",
            install_source="unknown",
            package_name="longhouse",
            channel="stable",
            installed_version="0.1.5",
            installed_at="2026-04-07T00:00:00+00:00",
            last_upgrade_at="2026-04-07T00:00:00+00:00",
        ),
    )
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.7")

    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_manager.subprocess, "run", fake_run)

    result = runner.invoke(app, ["upgrade", "--package-source", "/tmp/longhouse-0.1.7.whl"])

    assert result.exit_code == 0, result.output
    assert calls == [
        ["uv", "tool", "uninstall", "longhouse"],
        ["uv", "tool", "install", "--force", "--no-cache", "/tmp/longhouse-0.1.7.whl"],
    ]
    metadata_payload = update_manager.load_install_metadata()
    assert metadata_payload is not None
    assert metadata_payload.install_source == "custom"
    assert metadata_payload.package_ref == "/tmp/longhouse-0.1.7.whl"
    assert metadata_payload.installed_version == "0.1.7"


def test_record_install_command_writes_metadata(monkeypatch, tmp_path):
    runner = CliRunner()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    monkeypatch.setenv("HOME", str(runner_home))
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")

    result = runner.invoke(
        app,
        [
            "record-install",
            "--install-method",
            "uv",
            "--install-source",
            "pypi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"install_source": "pypi"' in result.output
    assert (runner_home / ".longhouse" / "install.json").exists()
