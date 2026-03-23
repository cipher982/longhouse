import json
from pathlib import Path

from zerg.cli import doctor


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
