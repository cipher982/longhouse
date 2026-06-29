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


def test_install_service_defaults_hosted_runtime_to_paused_archive_repair(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    log_dir = tmp_path / ".longhouse" / "agent" / "logs"
    launchd_path = tmp_path / "LaunchAgents" / "com.longhouse.shipper.plist"
    modes: list[str] = []

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
        lambda config: modes.append(config.archive_repair_mode)
        or {"success": True, "platform": "macos", "service": "com.longhouse.shipper", "message": "ok"},
    )

    shipper_service.install_service(
        url="https://david010.longhouse.ai",
        token=None,
        claude_dir=str(claude_dir),
    )

    assert modes == ["paused"]


def test_install_service_defaults_custom_runtime_to_drain_archive_repair(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    log_dir = tmp_path / ".longhouse" / "agent" / "logs"
    launchd_path = tmp_path / "LaunchAgents" / "com.longhouse.shipper.plist"
    modes: list[str] = []

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
        lambda config: modes.append(config.archive_repair_mode)
        or {"success": True, "platform": "macos", "service": "com.longhouse.shipper", "message": "ok"},
    )

    shipper_service.install_service(
        url="https://example.com",
        token=None,
        claude_dir=str(claude_dir),
    )

    assert modes == ["drain"]


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


def test_install_service_migrates_legacy_shipper_db_on_first_install(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    legacy_db_path = claude_dir / "longhouse-shipper.db"
    launchd_path = tmp_path / "LaunchAgents" / "com.longhouse.shipper.plist"
    log_dir = tmp_path / ".longhouse" / "agent" / "logs"
    install_calls: list[Path] = []

    legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_db_path.write_text("legacy-db", encoding="utf-8")
    Path(f"{legacy_db_path}-wal").write_text("legacy-wal", encoding="utf-8")
    Path(f"{legacy_db_path}-shm").write_text("legacy-shm", encoding="utf-8")

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

    migrated_db_path = tmp_path / ".longhouse" / "agent" / "longhouse-shipper.db"
    assert result["message"] == "ok"
    assert install_calls == [migrated_db_path]
    assert migrated_db_path.read_text(encoding="utf-8") == "legacy-db"
    assert Path(f"{migrated_db_path}-wal").read_text(encoding="utf-8") == "legacy-wal"
    assert Path(f"{migrated_db_path}-shm").read_text(encoding="utf-8") == "legacy-shm"
    assert not legacy_db_path.exists()
    assert not Path(f"{legacy_db_path}-wal").exists()
    assert not Path(f"{legacy_db_path}-shm").exists()


def test_install_service_reuses_legacy_shipper_db_on_reinstall_before_guard(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    legacy_db_path = claude_dir / "longhouse-shipper.db"
    launchd_path = tmp_path / "LaunchAgents" / "com.longhouse.shipper.plist"
    log_dir = tmp_path / ".longhouse" / "agent" / "logs"
    install_calls: list[Path] = []
    stop_calls: list[list[str]] = []

    launchd_path.parent.mkdir(parents=True, exist_ok=True)
    launchd_path.write_text("<plist/>", encoding="utf-8")
    legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_db_path.write_text("legacy-db", encoding="utf-8")

    monkeypatch.setattr(shipper_service, "detect_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(shipper_service, "_get_launchd_plist_path", lambda: launchd_path)
    monkeypatch.setattr(
        shipper_service,
        "_get_legacy_engine_plist_path",
        lambda: tmp_path / "LaunchAgents" / "com.longhouse.engine.plist",
    )
    monkeypatch.setattr(
        shipper_service.subprocess,
        "run",
        lambda args, **kwargs: stop_calls.append(list(args)),
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

    migrated_db_path = tmp_path / ".longhouse" / "agent" / "longhouse-shipper.db"
    assert result["message"] == "ok"
    assert install_calls == [migrated_db_path]
    assert migrated_db_path.read_text(encoding="utf-8") == "legacy-db"
    assert stop_calls == [["launchctl", "unload", str(launchd_path)]]


def test_launchd_plist_includes_prevent_sleep_when_enabled(monkeypatch):
    monkeypatch.setattr(shipper_service, "get_engine_executable", lambda: "/usr/local/bin/longhouse-engine")
    monkeypatch.setattr(shipper_service, "_resolve_log_dir", lambda config: Path("/tmp/logs"))
    monkeypatch.setattr(
        shipper_service,
        "resolve_longhouse_home_from_provider_home",
        lambda _: Path("/tmp/.longhouse"),
    )

    config = shipper_service.ServiceConfig(
        url="https://example.com",
        prevent_sleep=True,
    )
    plist = shipper_service._generate_launchd_plist(config)

    assert "--prevent-sleep" in plist


def test_launchd_plist_omits_prevent_sleep_by_default(monkeypatch):
    monkeypatch.setattr(shipper_service, "get_engine_executable", lambda: "/usr/local/bin/longhouse-engine")
    monkeypatch.setattr(shipper_service, "_resolve_log_dir", lambda config: Path("/tmp/logs"))
    monkeypatch.setattr(
        shipper_service,
        "resolve_longhouse_home_from_provider_home",
        lambda _: Path("/tmp/.longhouse"),
    )

    config = shipper_service.ServiceConfig(
        url="https://example.com",
    )
    plist = shipper_service._generate_launchd_plist(config)

    assert "--prevent-sleep" not in plist
