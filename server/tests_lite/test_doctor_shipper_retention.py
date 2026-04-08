import json
from pathlib import Path

from zerg.cli import doctor
from zerg.services import local_health_ui
from zerg.services import shipper
from zerg.services.shipper.service import Platform


def _seed_claude_dir(claude_dir: Path, settings: dict) -> None:
    (claude_dir / "projects" / "demo-project").mkdir(parents=True, exist_ok=True)
    (claude_dir / "longhouse-device-token").write_text("dev-token\n", encoding="utf-8")
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_check_shipper_warns_on_default_retention_and_missing_stop_hook(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    _seed_claude_dir(claude_dir, settings={})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    results = doctor._check_shipper()
    labels = {r.label: r.status for r in results}

    assert labels["cleanupPeriodDays not set (Claude default is ~30 days)"] == doctor.WARN
    assert labels["Claude Stop hook missing Longhouse shipper"] == doctor.WARN


def test_check_shipper_passes_with_long_retention_and_longhouse_stop_hook(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    _seed_claude_dir(
        claude_dir,
        settings={
            "cleanupPeriodDays": 180,
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": str(claude_dir / "hooks" / "longhouse-hook.sh")}
                        ]
                    }
                ]
            },
        },
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    results = doctor._check_shipper()
    labels = {r.label: r.status for r in results}

    assert labels["cleanupPeriodDays=180 days"] == doctor.PASS
    assert labels["Claude Stop hook includes Longhouse shipper"] == doctor.PASS


def test_check_shipper_reports_ambient_app_bundle(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    _seed_claude_dir(claude_dir, settings={})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setattr(shipper, "get_service_status", lambda: "running")
    monkeypatch.setattr(local_health_ui, "get_menubar_service_info", lambda: {
        "status": "running",
        "artifact_path": "/Users/test/Applications/Longhouse.app",
        "runtime_mode": "app-bundle",
    })
    monkeypatch.setattr("zerg.services.shipper.service.detect_platform", lambda: Platform.MACOS)

    results = doctor._check_shipper()
    labels = {r.label: r.status for r in results}

    assert labels["Ambient UI running"] == doctor.PASS
    assert labels["Ambient UI installed as Longhouse.app (/Users/test/Applications/Longhouse.app)"] == doctor.PASS


def test_check_shipper_warns_when_ambient_ui_uses_legacy_binary_install(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    _seed_claude_dir(claude_dir, settings={})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setattr(shipper, "get_service_status", lambda: "running")
    monkeypatch.setattr(local_health_ui, "get_menubar_service_info", lambda: {
        "status": "running",
        "artifact_path": "/Users/test/.local/bin/longhouse-local-health-menubar",
        "runtime_mode": "legacy-binary-install",
    })
    monkeypatch.setattr("zerg.services.shipper.service.detect_platform", lambda: Platform.MACOS)

    results = doctor._check_shipper()
    labels = {r.label: r.status for r in results}

    assert labels["Ambient UI using legacy binary install (/Users/test/.local/bin/longhouse-local-health-menubar)"] == doctor.WARN
