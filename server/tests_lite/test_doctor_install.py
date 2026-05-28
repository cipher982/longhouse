from __future__ import annotations

import json
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import config_file as config_file_cli
from zerg.cli import doctor as doctor_cli
from zerg.cli.main import app
from zerg.cli.update_manager import InstallMetadata
from zerg.cli.update_manager import UpdateCheckResult
from zerg.services import machine_state as machine_state_service
from zerg.services import shipper as shipper_service
from zerg.services.shipper.service import Platform as ServicePlatform


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


def test_check_provider_support_reports_capability_axes(monkeypatch):
    monkeypatch.setattr(
        "zerg.services.local_health.collect_local_health",
        lambda: {
            "provider_support_state": {
                "providers": {
                    "claude": {
                        "state": "ready",
                        "capabilities": {"live_control_operations": ["send", "steer"]},
                        "proof": {"minimum_evidence_level": "source_review"},
                        "version_readiness": {"state": "no_artifact"},
                        "live_proof": {
                            "status": "ok",
                            "version_match": "match",
                            "freshness_status": "fresh",
                            "verdict": "yellow",
                            "failure_code": "insufficient_coverage",
                        },
                    },
                    "opencode": {
                        "state": "provider_cli_missing",
                        "capabilities": {"live_control_operations": []},
                        "proof": {"minimum_evidence_level": "live_no_token"},
                        "version_readiness": {"state": "not_configured"},
                        "live_proof": {"status": "not_configured"},
                    },
                }
            }
        },
    )

    results = doctor_cli._check_provider_support()
    labels = {result.label: result for result in results}

    assert labels["claude managed support ready"].status == doctor_cli.PASS
    assert labels["claude managed support ready"].detail == (
        "live=send, steer; proof_min=source_review; version=no_artifact; "
        "local_proof=ok,version=match,freshness=fresh,verdict=yellow,failure=insufficient_coverage"
    )
    assert labels["opencode managed support provider_cli_missing"].status == doctor_cli.WARN


def test_check_provider_support_warns_on_stale_local_live_proof(monkeypatch):
    monkeypatch.setattr(
        "zerg.services.local_health.collect_local_health",
        lambda: {
            "provider_support_state": {
                "providers": {
                    "codex": {
                        "state": "ready",
                        "capabilities": {"live_control_operations": ["send", "interrupt"]},
                        "proof": {"minimum_evidence_level": "hermetic"},
                        "version_readiness": {"state": "installed_release_reviewed"},
                        "live_proof": {
                            "status": "stale",
                            "version_match": "match",
                            "freshness_status": "stale",
                            "verdict": "yellow",
                        },
                    }
                }
            }
        },
    )

    results = doctor_cli._check_provider_support()

    assert len(results) == 1
    assert results[0].status == doctor_cli.WARN
    assert results[0].label == "codex managed support ready"
    assert results[0].detail == (
        "live=send, interrupt; proof_min=hermetic; version=installed_release_reviewed; "
        "local_proof=stale,version=match,freshness=stale,verdict=yellow"
    )


def test_check_config_does_not_flag_hosted_machine_state_as_local_url_drift(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[server]\nhost = "127.0.0.1"\nport = 65534\n', encoding="utf-8")

    monkeypatch.setattr(config_file_cli, "get_config_path", lambda: config_path)
    monkeypatch.setattr(
        config_file_cli,
        "load_config",
        lambda config_path=None: SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=65534)),
    )
    monkeypatch.setattr(
        machine_state_service,
        "load_machine_state",
        lambda: machine_state_service.MachineState(
            runtime_url="https://david010.longhouse.ai",
            machine_name="cinder",
        ),
    )
    monkeypatch.setattr(
        "zerg.services.local_health.collect_launch_readiness",
        lambda: {
            "reasons": [],
            "runner": {"runner_urls": ["https://david010.longhouse.ai"], "runner_name": "cinder"},
            "control_plane_url": "https://david010.longhouse.ai",
            "machine_name": "cinder",
        },
    )

    results = doctor_cli._check_config()

    assert not any(result.label.startswith("URL drift:") for result in results)


def test_check_config_tolerates_missing_machine_state(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[server]\nhost = "127.0.0.1"\nport = 65534\n', encoding="utf-8")

    monkeypatch.setattr(config_file_cli, "get_config_path", lambda: config_path)
    monkeypatch.setattr(
        config_file_cli,
        "load_config",
        lambda config_path=None: SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=65534)),
    )
    monkeypatch.setattr(machine_state_service, "load_machine_state", lambda: None)
    monkeypatch.setattr(
        "zerg.services.local_health.collect_launch_readiness",
        lambda: {
            "reasons": [],
            "runner": {"runner_urls": [], "runner_name": ""},
            "control_plane_url": "",
            "machine_name": "",
        },
    )

    results = doctor_cli._check_config()

    assert isinstance(results, list)


def test_check_shipper_prefers_machine_repair_for_configured_machine(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "cleanupPeriodDays": 90,
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "command": "/Users/test/.local/bin/longhouse-stop-hook",
                                }
                            ]
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    token_path = claude_dir / "token"
    token_path.write_text("zdt_test", encoding="utf-8")
    machine_state_service.write_machine_state(
        base_dir=tmp_path / ".longhouse",
        written_by="test",
        runtime_url="https://demo.longhouse.test",
        machine_name="cinder",
    )

    monkeypatch.setattr(doctor_cli, "_get_claude_dir", lambda: claude_dir)
    monkeypatch.setattr(doctor_cli, "get_token_path", lambda _: token_path)
    monkeypatch.setattr(shipper_service, "get_service_status", lambda: "stopped")
    monkeypatch.setattr("zerg.services.shipper.service.detect_platform", lambda: ServicePlatform.LINUX)

    results = doctor_cli._check_shipper()

    service_result = next(result for result in results if result.label == "Machine agent service stopped")
    assert service_result.detail == "Run: longhouse machine repair"


def test_check_shipper_falls_back_to_connect_install_when_machine_unconfigured(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "cleanupPeriodDays": 90,
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "command": "/Users/test/.local/bin/longhouse-stop-hook",
                                }
                            ]
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    token_path = claude_dir / "token"
    token_path.write_text("zdt_test", encoding="utf-8")

    monkeypatch.setattr(doctor_cli, "_get_claude_dir", lambda: claude_dir)
    monkeypatch.setattr(doctor_cli, "get_token_path", lambda _: token_path)
    monkeypatch.setattr(shipper_service, "get_service_status", lambda: "stopped")
    monkeypatch.setattr("zerg.services.shipper.service.detect_platform", lambda: ServicePlatform.LINUX)

    results = doctor_cli._check_shipper()

    service_result = next(result for result in results if result.label == "Machine agent service stopped")
    assert service_result.detail == "Run: longhouse connect --install"
