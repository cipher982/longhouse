"""Tests for machine-agent reinstall safety around local shipper state."""

from pathlib import Path

import pytest

import zerg.services.shipper.service as shipper_service
from zerg.services.shipper.service import Platform


def test_install_service_allows_first_install_without_existing_shipper_db(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    log_dir = tmp_path / ".longhouse" / "agent" / "logs"
    launchd_path = tmp_path / "LaunchAgents" / "com.longhouse.shipper.plist"
    install_calls: list[str] = []

    monkeypatch.setattr(shipper_service, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(shipper_service, "_get_launchd_plist_path", lambda: launchd_path)
    monkeypatch.setattr(
        shipper_service,
        "_get_legacy_engine_plist_path",
        lambda: tmp_path / "LaunchAgents" / "com.longhouse.engine.plist",
    )
    monkeypatch.setattr(shipper_service, "_resolve_log_dir", lambda config: log_dir)
    monkeypatch.setattr(
        shipper_service,
        "_install_launchd",
        lambda config: install_calls.append(config.url)
        or {
            "success": True,
            "platform": "macos",
            "service": "com.longhouse.shipper",
            "plist_path": str(launchd_path),
            "message": "ok",
        },
    )

    result = shipper_service.install_service(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
    )

    assert result["message"] == "ok"
    assert install_calls == ["https://example.com"]
    assert log_dir.exists()


def test_install_service_refuses_reinstall_when_existing_service_lost_shipper_db(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    launchd_path = tmp_path / "LaunchAgents" / "com.longhouse.shipper.plist"
    launchd_path.parent.mkdir(parents=True, exist_ok=True)
    launchd_path.write_text("<plist/>", encoding="utf-8")

    monkeypatch.setattr(shipper_service, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(shipper_service, "_get_launchd_plist_path", lambda: launchd_path)
    monkeypatch.setattr(
        shipper_service,
        "_get_legacy_engine_plist_path",
        lambda: tmp_path / "LaunchAgents" / "com.longhouse.engine.plist",
    )

    with pytest.raises(RuntimeError) as exc:
        shipper_service.install_service(
            url="https://example.com",
            token=None,
            claude_dir=str(claude_dir),
        )

    message = str(exc.value)
    assert "missing its shipper state DB" in message
    assert str(tmp_path / ".longhouse" / "agent" / "longhouse-shipper.db") in message


def test_install_service_allows_reinstall_when_existing_shipper_db_present(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    launchd_path = tmp_path / "LaunchAgents" / "com.longhouse.shipper.plist"
    db_path = tmp_path / ".longhouse" / "agent" / "longhouse-shipper.db"
    log_dir = tmp_path / ".longhouse" / "agent" / "logs"
    install_calls: list[Path] = []

    launchd_path.parent.mkdir(parents=True, exist_ok=True)
    launchd_path.write_text("<plist/>", encoding="utf-8")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(shipper_service, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(shipper_service, "_get_launchd_plist_path", lambda: launchd_path)
    monkeypatch.setattr(
        shipper_service,
        "_get_legacy_engine_plist_path",
        lambda: tmp_path / "LaunchAgents" / "com.longhouse.engine.plist",
    )
    monkeypatch.setattr(shipper_service, "_resolve_log_dir", lambda config: log_dir)
    monkeypatch.setattr(
        shipper_service,
        "_install_launchd",
        lambda config: install_calls.append(Path(shipper_service._resolve_agent_db_path(config)))
        or {
            "success": True,
            "platform": "macos",
            "service": "com.longhouse.shipper",
            "plist_path": str(launchd_path),
            "message": "ok",
        },
    )

    result = shipper_service.install_service(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
    )

    assert result["message"] == "ok"
    assert install_calls == [db_path]
