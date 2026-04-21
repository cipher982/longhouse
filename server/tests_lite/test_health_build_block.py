"""/api/health must surface the build identity block and flip to
unhealthy when build identity is missing."""

from __future__ import annotations

import json
import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg import build_info


VALID_PAYLOAD = {
    "version": "9.9.9",
    "commit": "cafef00dcafef00dcafef00dcafef00dcafef00d",
    "commit_short": "cafef00d",
    "dirty": True,
    "built_at": "2026-04-21T18:03:12Z",
    "channel": "dev",
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


@pytest.fixture
def client():
    from zerg.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_cache():
    build_info.reset_cache()
    yield
    build_info.reset_cache()


def test_health_exposes_build_block(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resource(monkeypatch, VALID_PAYLOAD)

    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["build"]["commit_short"] == "cafef00d"
    assert body["build"]["channel"] == "dev"
    assert body["build"]["dirty"] is True


def test_health_unhealthy_when_build_identity_missing(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resource(monkeypatch, None)

    resp = client.get("/api/health")
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["build"]["error"] == "missing"
