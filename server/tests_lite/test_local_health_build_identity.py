"""Tests for local build identity attribution and restart-pending evidence.

Python package, installed native pair, and running engine are independent.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import build_info
from zerg.services import local_health as local_health_service
from zerg.services.local_health import engine_status as engine_status_service


PYTHON_PACKAGE_PAYLOAD = {
    "version": "0.2.0",
    "commit": "aaaaaaaa1111111111111111111111111111bbbb",
    "commit_short": "aaaaaaaa",
    "dirty": False,
    "built_at": "2026-04-21T18:03:12Z",
    "channel": "release",
}

NATIVE_ENGINE_PAYLOAD = {
    "version": "0.1.64",
    "commit": "bbbbbbbb2222222222222222222222222222cccc",
    "commit_short": "bbbbbbbb",
    "dirty": False,
    "built_at": "2026-04-21T18:03:12Z",
    "channel": "release",
}

CLI_PAYLOAD = PYTHON_PACKAGE_PAYLOAD


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


def _install_resource(monkeypatch: pytest.MonkeyPatch, payload: dict | None) -> None:
    raw = None if payload is None else json.dumps(payload)
    monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _FakeResource(raw))
    build_info.reset_cache()


def _native_identity(engine: dict | None = None, *, error: str | None = None) -> dict:
    if error is not None:
        return {"error": error, "detail": f"native identity {error}"}
    return {
        "path": "/Users/example/.local/bin/longhouse",
        "engine_path": "/Users/example/.local/bin/longhouse-engine",
        "facade": {**NATIVE_ENGINE_PAYLOAD},
        "engine": {**(engine or NATIVE_ENGINE_PAYLOAD)},
    }


def _engine_status(build: dict | None, *, binary_mtime: str | None = None, daemon_started_at: str | None = None) -> dict:
    payload: dict = {"version": "0.2.0"}
    if build is not None:
        payload["build"] = build
    if binary_mtime is not None:
        payload["binary_mtime"] = binary_mtime
    if daemon_started_at is not None:
        payload["daemon_started_at"] = daemon_started_at
    return {"path": "/tmp/engine-status.json", "exists": True, "payload": payload, "error": None}


@pytest.fixture(autouse=True)
def _reset_cache():
    build_info.reset_cache()
    yield
    build_info.reset_cache()


@pytest.fixture
def _install_cli_identity(monkeypatch: pytest.MonkeyPatch):
    _install_resource(monkeypatch, CLI_PAYLOAD)
    monkeypatch.setattr(
        engine_status_service,
        "_collect_installed_native_identity",
        lambda *, engine_payload: _native_identity(),
    )


def test_python_package_update_does_not_mark_matching_native_engine_restart_pending(_install_cli_identity) -> None:
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status({**NATIVE_ENGINE_PAYLOAD}),
    )

    assert result["engine_restart_pending"] is False
    assert result["python_package"]["commit_short"] == "aaaaaaaa"
    assert result["installed_native"]["facade"]["commit_short"] == "bbbbbbbb"
    assert result["installed_native"]["engine"]["commit_short"] == "bbbbbbbb"
    assert result["running_engine"]["commit_short"] == "bbbbbbbb"
    assert result["restart_pending_reasons"] == {
        "native_engine_commit_mismatch": False,
        "binary_newer_than_daemon": None,
    }
    assert {c["name"] for c in result["components"]} == {
        "python_package",
        "native_facade",
        "installed_native_engine",
        "running_engine",
    }


def test_flags_restart_pending_when_installed_native_engine_differs(_install_cli_identity) -> None:
    engine_build = {
        **NATIVE_ENGINE_PAYLOAD,
        "commit": "cccccccc3333333333333333333333333333dddd",
        "commit_short": "cccccccc",
    }
    result = local_health_service._collect_build_identity(engine_status=_engine_status(engine_build))

    assert result["engine_restart_pending"] is True
    assert result["installed_native"]["engine"]["commit_short"] == "bbbbbbbb"
    assert result["running_engine"]["commit_short"] == "cccccccc"
    assert result["restart_pending_reasons"]["native_engine_commit_mismatch"] is True


@pytest.mark.parametrize(
    ("commit", "commit_short", "restart_pending"),
    [
        (NATIVE_ENGINE_PAYLOAD["commit"], "bbbb", False),
        ("bbbbbbbb3333333333333333333333333333dddd", "bbbbbbbb", True),
    ],
)
def test_restart_identity_uses_full_commits(_install_cli_identity, commit, commit_short, restart_pending) -> None:
    running = {**NATIVE_ENGINE_PAYLOAD, "commit": commit, "commit_short": commit_short}
    result = local_health_service._collect_build_identity(engine_status=_engine_status(running))

    assert result["engine_restart_pending"] is restart_pending
    assert result["restart_pending_reasons"]["native_engine_commit_mismatch"] is restart_pending


def test_engine_missing_build_block_does_not_mark_restart_pending(_install_cli_identity) -> None:
    """A missing running identity stays unknown, not a false mismatch."""
    result = local_health_service._collect_build_identity(engine_status=_engine_status(None))

    assert result["engine_restart_pending"] is None
    assert result["running_engine"] is None
    assert result["restart_pending_reasons"]["native_engine_commit_mismatch"] is None
    names = {c["name"] for c in result["components"]}
    assert names == {"python_package", "native_facade", "installed_native_engine"}


def test_engine_payload_not_a_mapping_is_tolerated(_install_cli_identity) -> None:
    """Corrupt engine-status.json must not crash local-health."""
    engine_status = {"path": "/tmp/x", "exists": True, "payload": "nonsense", "error": None}
    result = local_health_service._collect_build_identity(engine_status=engine_status)

    assert result["engine_restart_pending"] is None
    assert result["running_engine"] is None
    assert result["installed_native"]["engine"]["commit_short"] == "bbbbbbbb"


def test_engine_build_not_a_mapping_is_tolerated(_install_cli_identity) -> None:
    engine_status = {
        "path": "/tmp/x",
        "exists": True,
        "payload": {"version": "0.2.0", "build": ["unexpected", "shape"]},
        "error": None,
    }
    result = local_health_service._collect_build_identity(engine_status=engine_status)

    assert result["engine_restart_pending"] is None
    assert result["running_engine"] is None


def test_missing_native_identity_is_explicitly_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resource(monkeypatch, PYTHON_PACKAGE_PAYLOAD)
    monkeypatch.setattr(
        engine_status_service,
        "_collect_installed_native_identity",
        lambda *, engine_payload: _native_identity(error="unavailable"),
    )
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status({**NATIVE_ENGINE_PAYLOAD}),
    )

    assert result["installed_native"]["error"] == "unavailable"
    assert result["running_engine"]["commit_short"] == "bbbbbbbb"
    assert result["engine_restart_pending"] is None
    assert result["restart_pending_reasons"]["native_engine_commit_mismatch"] is None


def test_corrupt_native_identity_is_explicitly_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resource(monkeypatch, PYTHON_PACKAGE_PAYLOAD)
    monkeypatch.setattr(
        engine_status_service,
        "_collect_installed_native_identity",
        lambda *, engine_payload: _native_identity(error="corrupt"),
    )
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status({**NATIVE_ENGINE_PAYLOAD}),
    )

    assert result["installed_native"]["error"] == "corrupt"
    assert result["restart_pending_reasons"]["native_engine_commit_mismatch"] is None


def test_incomplete_native_identity_cannot_claim_a_match(_install_cli_identity, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        engine_status_service,
        "_collect_installed_native_identity",
        lambda *, engine_payload: _native_identity(engine={"commit_short": "bbbbbbbb"}),
    )
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status({**NATIVE_ENGINE_PAYLOAD}),
    )

    assert result["engine_restart_pending"] is None
    assert result["restart_pending_reasons"]["native_engine_commit_mismatch"] is None


def test_replaced_native_binary_still_reports_restart_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resource(monkeypatch, PYTHON_PACKAGE_PAYLOAD)
    monkeypatch.setattr(
        engine_status_service,
        "_collect_installed_native_identity",
        lambda *, engine_payload: _native_identity(error="unavailable"),
    )
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status(
            {**NATIVE_ENGINE_PAYLOAD},
            binary_mtime="2026-09-11T12:00:10Z",
            daemon_started_at="2026-09-11T12:00:00Z",
        ),
    )

    assert result["engine_restart_pending"] is True
    assert result["restart_pending_reasons"]["native_engine_commit_mismatch"] is None
    assert result["restart_pending_reasons"]["binary_newer_than_daemon"] is True


def test_cli_identity_missing_surfaces_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resource(monkeypatch, None)
    monkeypatch.setattr(
        engine_status_service,
        "_collect_installed_native_identity",
        lambda *, engine_payload: _native_identity(error="unavailable"),
    )

    engine_build = {
        **NATIVE_ENGINE_PAYLOAD,
        "commit": "ccccc1113333333333333333333333333333dddd",
        "commit_short": "ccccc111",
    }
    result = local_health_service._collect_build_identity(engine_status=_engine_status(engine_build))

    assert result["python_package"]["error"] == "missing"
    assert result["installed_native"]["error"] == "unavailable"
    assert result["engine_restart_pending"] is None
    names = [c["name"] for c in result["components"]]
    assert names == ["running_engine"]


def test_fresh_pulse_does_not_make_old_projection_fresh(tmp_path, monkeypatch) -> None:
    status_path = tmp_path / "engine-status.json"
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    status_path.write_text(
        json.dumps(
            {
                "local_projection": {
                    "generated_at": (now - timedelta(seconds=120)).isoformat(),
                    "engine_pulse_at": (now - timedelta(seconds=5)).isoformat(),
                    "last_reconciled_at": (now - timedelta(seconds=120)).isoformat(),
                    "reconciliation": {"state": "idle"},
                }
            }
        )
    )
    monkeypatch.setattr(engine_status_service, "get_agent_status_path", lambda _base_dir: status_path)

    result = engine_status_service._collect_engine_status(tmp_path, now=now)

    assert result["fresh"] is True
    assert result["age_seconds"] == 5
    assert result["projection_age_seconds"] == 120
    assert result["projection_stale"] is True
