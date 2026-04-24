"""Tests for `_collect_build_identity` — the installed/engine restart-pending
detector that feeds the local-health snapshot and menu bar."""

from __future__ import annotations

import json
import os

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import build_info
from zerg.services import local_health as local_health_service


CLI_PAYLOAD = {
    "version": "0.2.0",
    "commit": "aaaaaaaa1111111111111111111111111111bbbb",
    "commit_short": "aaaaaaaa",
    "dirty": False,
    "built_at": "2026-04-21T18:03:12Z",
    "channel": "release",
}


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


def _engine_status(build: dict | None) -> dict:
    payload: dict = {"version": "0.2.0"}
    if build is not None:
        payload["build"] = build
    return {"path": "/tmp/engine-status.json", "exists": True, "payload": payload, "error": None}


@pytest.fixture(autouse=True)
def _reset_cache():
    build_info.reset_cache()
    yield
    build_info.reset_cache()


@pytest.fixture
def _install_cli_identity(monkeypatch: pytest.MonkeyPatch):
    _install_resource(monkeypatch, CLI_PAYLOAD)


def test_no_restart_pending_when_installed_and_engine_agree(_install_cli_identity) -> None:
    engine_build = {**CLI_PAYLOAD}
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status(engine_build)
    )

    assert result["engine_restart_pending"] is False
    assert result["installed"]["commit_short"] == "aaaaaaaa"
    assert result["engine"]["commit_short"] == "aaaaaaaa"
    names = {c["name"] for c in result["components"]}
    assert names == {"installed", "engine"}


def test_flags_restart_pending_when_short_shas_differ(_install_cli_identity) -> None:
    engine_build = {**CLI_PAYLOAD, "commit_short": "bbbbbbbb"}
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status(engine_build)
    )

    assert result["engine_restart_pending"] is True
    assert result["installed"]["commit_short"] == "aaaaaaaa"
    assert result["engine"]["commit_short"] == "bbbbbbbb"


def test_engine_missing_build_block_does_not_mark_restart_pending(_install_cli_identity) -> None:
    """An engine that predates the build block still registers as "same" —
    we only flag restart pending when we have two short SHAs that disagree."""
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status(None)
    )

    assert result["engine_restart_pending"] is False
    assert result["engine"] is None
    names = [c["name"] for c in result["components"]]
    assert names == ["installed"]


def test_engine_payload_not_a_mapping_is_tolerated(_install_cli_identity) -> None:
    """Corrupt engine-status.json whose payload is not an object must
    not crash local-health — treat it as "no engine build block"."""
    engine_status = {"path": "/tmp/x", "exists": True, "payload": "nonsense", "error": None}
    result = local_health_service._collect_build_identity(engine_status=engine_status)

    assert result["engine_restart_pending"] is False
    assert result["engine"] is None
    assert [c["name"] for c in result["components"]] == ["installed"]


def test_engine_build_not_a_mapping_is_tolerated(_install_cli_identity) -> None:
    engine_status = {
        "path": "/tmp/x",
        "exists": True,
        "payload": {"version": "0.2.0", "build": ["unexpected", "shape"]},
        "error": None,
    }
    result = local_health_service._collect_build_identity(engine_status=engine_status)

    assert result["engine_restart_pending"] is False
    assert result["engine"] is None


def test_cli_identity_missing_surfaces_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resource(monkeypatch, None)

    engine_build = {**CLI_PAYLOAD, "commit_short": "ccccc111"}
    result = local_health_service._collect_build_identity(
        engine_status=_engine_status(engine_build)
    )

    assert result["installed"]["error"] == "missing"
    # With CLI missing we only have one short SHA — nothing to compare.
    assert result["engine_restart_pending"] is False
    names = [c["name"] for c in result["components"]]
    assert names == ["engine"]
