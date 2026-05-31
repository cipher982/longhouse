from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import build_info
from zerg.cli.main import app
from zerg.cli import update_manager


class _FakeResource:
    def __init__(self, raw: str | None) -> None:
        self._raw = raw

    def is_file(self) -> bool:
        return self._raw is not None

    def read_text(self, encoding: str = "utf-8") -> str:
        assert self._raw is not None
        return self._raw

    def __truediv__(self, _other: str) -> "_FakeResource":
        return self


def _install_fake_build_identity(tmp_path: Path, monkeypatch, **overrides) -> dict:
    del tmp_path  # kept for signature parity; importlib.resources path used instead
    payload = {
        "version": "0.1.5",
        "commit": "cafebabedeadbeefcafebabedeadbeefcafebabe",
        "commit_short": "cafebabe",
        "dirty": False,
        "built_at": "2026-04-21T18:03:12Z",
        "channel": "release",
    }
    payload.update(overrides)
    raw = json.dumps(payload)
    monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _FakeResource(raw))
    build_info.reset_cache()
    return payload


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


def test_detect_install_metadata_defaults_to_pypi_when_runtime_has_no_direct_source(monkeypatch):
    monkeypatch.setattr(update_manager, "load_install_metadata", lambda: None)
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")
    monkeypatch.setattr(
        update_manager,
        "_probe_installed_distribution",
        lambda package_name="longhouse": update_manager.DistributionInstallProbe(install_method="uv"),
    )

    metadata_payload = update_manager.detect_install_metadata()

    assert metadata_payload.install_method == "uv"
    assert metadata_payload.install_source == "pypi"
    assert metadata_payload.package_ref is None


def test_detect_install_metadata_prefers_runtime_editable_path_over_stale_record(monkeypatch):
    monkeypatch.setattr(
        update_manager,
        "load_install_metadata",
        lambda: update_manager.InstallMetadata(
            install_method="uv",
            install_source="pypi",
            package_name="longhouse",
            channel="stable",
            installed_version="0.1.5",
            installed_at="2026-04-07T00:00:00+00:00",
            last_upgrade_at="2026-04-07T00:00:00+00:00",
            package_ref=None,
        ),
    )
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.8")
    monkeypatch.setattr(
        update_manager,
        "_probe_installed_distribution",
        lambda package_name="longhouse": update_manager.DistributionInstallProbe(
            install_method="uv",
            install_source="editable-path",
            package_ref="/Users/example/git/zerg/longhouse/server",
        ),
    )

    metadata_payload = update_manager.detect_install_metadata()

    assert metadata_payload.install_method == "uv"
    assert metadata_payload.install_source == "editable-path"
    assert metadata_payload.package_ref == "/Users/example/git/zerg/longhouse/server"
    assert metadata_payload.installed_version == "0.1.8"
    assert metadata_payload.installed_at == "2026-04-07T00:00:00+00:00"


def test_version_command_reports_installed_version(monkeypatch, tmp_path):
    _install_fake_build_identity(tmp_path, monkeypatch)
    runner = CliRunner()

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.1.5 (cafebabe)"


def test_version_command_reports_dev_dirty(monkeypatch, tmp_path):
    _install_fake_build_identity(tmp_path, monkeypatch, channel="dev", dirty=True)
    runner = CliRunner()

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.1.5-dev+cafebabe.dirty"


def test_version_command_json_includes_build_block(monkeypatch, tmp_path):
    payload = _install_fake_build_identity(tmp_path, monkeypatch)
    runner = CliRunner()

    result = runner.invoke(app, ["version", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["installed_version"] == "0.1.5 (cafebabe)"
    assert data["build"] == payload


def test_version_command_reports_missing_build_identity(monkeypatch):
    monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _FakeResource(None))
    build_info.reset_cache()

    runner = CliRunner()
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 2, result.output
    assert "build identity missing" in result.output


def test_version_command_check_reports_update(monkeypatch, tmp_path):
    _install_fake_build_identity(tmp_path, monkeypatch)
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
    assert "Installed: 0.1.5 (cafebabe)" in result.output
    assert "Latest:    0.1.6" in result.output
    assert "Update available." in result.output
    assert "uv tool upgrade longhouse" in result.output


def test_version_command_check_returns_json_error_on_failure(monkeypatch, tmp_path):
    _install_fake_build_identity(tmp_path, monkeypatch)
    runner = CliRunner()
    monkeypatch.setattr(update_manager, "check_for_updates", lambda package_name="longhouse": (_ for _ in ()).throw(RuntimeError("boom")))

    result = runner.invoke(app, ["version", "--check", "--json"])

    assert result.exit_code == 1, result.output
    assert '"installed_version": "0.1.5 (cafebabe)"' in result.output
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
    monkeypatch.setattr(
        update_manager,
        "_probe_installed_distribution",
        lambda package_name="longhouse": update_manager.DistributionInstallProbe(install_method="uv"),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_manager.subprocess, "run", fake_run)
    monkeypatch.setattr(update_manager, "_reconcile_runtime_after_upgrade", lambda: None)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "upgrade", "longhouse"]]
    metadata_payload = update_manager.load_install_metadata()
    assert metadata_payload is not None
    assert metadata_payload.install_source == "pypi"
    assert metadata_payload.installed_version == "0.1.6"


def test_upgrade_command_rewrites_stale_metadata_from_runtime_probe(monkeypatch, tmp_path):
    runner = CliRunner()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    monkeypatch.setenv("HOME", str(runner_home))
    stale_install_json = runner_home / ".longhouse" / "install.json"
    stale_install_json.parent.mkdir(parents=True)
    stale_install_json.write_text(
        """
{
  "install_method": "uv",
  "install_source": "pypi",
  "package_name": "longhouse",
  "channel": "stable",
  "installed_version": "0.1.5",
  "installed_at": "2026-04-07T00:00:00+00:00",
  "last_upgrade_at": "2026-04-07T00:00:00+00:00"
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.8")

    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_manager.subprocess, "run", fake_run)
    monkeypatch.setattr(
        update_manager,
        "_probe_installed_distribution",
        lambda package_name="longhouse": update_manager.DistributionInstallProbe(
            install_method="uv",
            install_source="editable-path",
            package_ref="/Users/example/git/zerg/longhouse/server",
        ),
    )
    monkeypatch.setattr(update_manager, "_reconcile_runtime_after_upgrade", lambda: None)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "upgrade", "longhouse"]]
    metadata_payload = update_manager.load_install_metadata()
    assert metadata_payload is not None
    assert metadata_payload.install_source == "editable-path"
    assert metadata_payload.package_ref == "/Users/example/git/zerg/longhouse/server"
    assert metadata_payload.installed_version == "0.1.8"


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
    monkeypatch.setattr(update_manager, "_reconcile_runtime_after_upgrade", lambda: None)

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


def test_upgrade_command_invokes_machine_repair_after_pypi_bump(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        update_manager,
        "_probe_installed_distribution",
        lambda package_name="longhouse": update_manager.DistributionInstallProbe(install_method="uv"),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_manager.subprocess, "run", fake_run)
    monkeypatch.setattr(update_manager, "_resolve_longhouse_entrypoint", lambda: "/usr/local/bin/longhouse")

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert calls == [
        ["uv", "tool", "upgrade", "longhouse"],
        ["/usr/local/bin/longhouse", "machine", "repair"],
    ]


def test_upgrade_skips_runtime_reconcile_when_entrypoint_missing(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        update_manager,
        "_probe_installed_distribution",
        lambda package_name="longhouse": update_manager.DistributionInstallProbe(install_method="uv"),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, check=False):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update_manager.subprocess, "run", fake_run)
    monkeypatch.setattr(update_manager, "_resolve_longhouse_entrypoint", lambda: None)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "upgrade", "longhouse"]]
    assert "longhouse machine repair" in result.output


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


def test_check_for_updates_falls_back_without_packaging(monkeypatch):
    monkeypatch.setattr(update_manager, "detect_install_metadata", lambda: update_manager.InstallMetadata(
        install_method="uv",
        install_source="pypi",
        package_name="longhouse",
        channel="stable",
        installed_version="0.1.5",
        installed_at="2026-04-07T00:00:00+00:00",
        last_upgrade_at="2026-04-07T00:00:00+00:00",
    ))
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")
    monkeypatch.setattr(update_manager, "fetch_latest_pypi_version", lambda package_name="longhouse": "0.1.6")

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "packaging.version":
            raise ModuleNotFoundError("No module named 'packaging'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(update_manager, "__import__", fake_import, raising=False)
    monkeypatch.setattr(sys.modules["builtins"], "__import__", fake_import)
    try:
        result = update_manager.check_for_updates()
    finally:
        monkeypatch.setattr(sys.modules["builtins"], "__import__", real_import)

    assert result.update_available is True


def test_maybe_notify_update_emits_cached_notice_without_spawning(monkeypatch):
    notices: list[str] = []
    spawns: list[bool] = []

    monkeypatch.setattr(update_manager.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")
    monkeypatch.setattr(
        update_manager,
        "load_update_cache",
        lambda: update_manager.CachedUpdateCheck(
            checked_at=update_manager._utc_now_iso(),
            installed_version="0.1.5",
            latest_version="0.1.6",
            update_available=True,
            upgrade_command="uv tool upgrade longhouse",
            install_method="uv",
            install_source="pypi",
            package_name="longhouse",
        ),
    )
    monkeypatch.setattr(update_manager, "spawn_background_update_check", lambda: spawns.append(True))
    monkeypatch.setattr(update_manager.typer, "secho", lambda message, **kwargs: notices.append(message))

    update_manager.maybe_notify_update(["serve"])

    assert notices == ["Update available: Longhouse 0.1.6 (you have 0.1.5). Run: uv tool upgrade longhouse"]
    assert spawns == []


def test_maybe_notify_update_spawns_background_refresh_for_stale_cache(monkeypatch):
    spawns: list[bool] = []

    monkeypatch.setattr(update_manager.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(update_manager, "current_installed_version", lambda package_name="longhouse": "0.1.5")
    monkeypatch.setattr(update_manager, "load_update_cache", lambda: None)
    monkeypatch.setattr(update_manager, "spawn_background_update_check", lambda: spawns.append(True) or True)
    monkeypatch.setattr(update_manager.typer, "secho", lambda message, **kwargs: None)

    update_manager.maybe_notify_update(["serve"])

    assert spawns == [True]


def test_maybe_notify_update_skips_json_and_update_commands(monkeypatch):
    spawns: list[str] = []

    monkeypatch.setattr(update_manager.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(update_manager, "spawn_background_update_check", lambda: spawns.append("spawned") or True)

    update_manager.maybe_notify_update(["wall", "--json"])
    update_manager.maybe_notify_update(["doctor"])
    update_manager.maybe_notify_update(["version", "--check"])

    assert spawns == []
