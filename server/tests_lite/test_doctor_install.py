from __future__ import annotations

import os

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import doctor as doctor_cli
from zerg.cli.main import app
from zerg.cli.update_manager import InstallMetadata
from zerg.cli.update_manager import UpdateCheckResult


def test_doctor_reports_install_metadata(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(doctor_cli, "_check_environment", lambda: [])
    monkeypatch.setattr(doctor_cli, "_check_server", lambda: [])
    monkeypatch.setattr(doctor_cli, "_check_shipper", lambda: [])
    monkeypatch.setattr(doctor_cli, "_check_config", lambda: [])
    monkeypatch.setattr(doctor_cli, "current_installed_version", lambda: "0.1.5")
    monkeypatch.setattr(
        doctor_cli,
        "load_install_metadata",
        lambda: InstallMetadata(
            install_method="uv",
            install_source="pypi",
            package_name="longhouse",
            channel="stable",
            installed_version="0.1.5",
            installed_at="2026-04-07T00:00:00+00:00",
            last_upgrade_at="2026-04-07T00:00:00+00:00",
        ),
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Install" in result.output
    assert "Longhouse CLI v0.1.5" in result.output
    assert "Install metadata present" in result.output


def test_doctor_check_updates_surfaces_upgrade_command(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(doctor_cli, "_check_environment", lambda: [])
    monkeypatch.setattr(doctor_cli, "_check_server", lambda: [])
    monkeypatch.setattr(doctor_cli, "_check_shipper", lambda: [])
    monkeypatch.setattr(doctor_cli, "_check_config", lambda: [])
    monkeypatch.setattr(doctor_cli, "current_installed_version", lambda: "0.1.5")
    monkeypatch.setattr(
        doctor_cli,
        "load_install_metadata",
        lambda: InstallMetadata(
            install_method="uv",
            install_source="pypi",
            package_name="longhouse",
            channel="stable",
            installed_version="0.1.5",
            installed_at="2026-04-07T00:00:00+00:00",
            last_upgrade_at="2026-04-07T00:00:00+00:00",
        ),
    )
    monkeypatch.setattr(
        doctor_cli,
        "check_for_updates",
        lambda: UpdateCheckResult(
            installed_version="0.1.5",
            latest_version="0.1.6",
            update_available=True,
            install_method="uv",
            install_source="pypi",
            upgrade_command="uv tool upgrade longhouse",
            package_name="longhouse",
        ),
    )

    result = runner.invoke(app, ["doctor", "--check-updates"])

    assert result.exit_code == 0, result.output
    assert "Update available (latest v0.1.6)" in result.output
    assert "uv tool upgrade longhouse" in result.output
