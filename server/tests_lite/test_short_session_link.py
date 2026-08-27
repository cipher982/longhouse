"""Tests for the /s/<prefix> short session-link redirect used by the CLI launch panel."""

from __future__ import annotations

import os
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")

from fastapi.testclient import TestClient  # noqa: E402

# The redirect resolves the prefix through catalogd, so a test that reaches the
# resolver needs a real one. /s/<prefix> lives on the root app, not api_app, so
# these build their own client instead of taking live_catalog_client.
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from zerg.main import app  # noqa: E402


def _client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_short_link_redirects_a_unique_prefix_resolution(monkeypatch):
    session_id = uuid4()
    monkeypatch.setattr(
        "zerg.services.catalog_read_gateway.resolve_session_prefix",
        lambda _prefix: {
            "status": "unique",
            "session": {"session_id": str(session_id)},
            "owner": None,
        },
    )

    response = _client().get(f"/s/{str(session_id).split('-')[0]}")

    assert response.status_code == 302
    assert response.headers["location"] == f"/timeline/{session_id}"


def test_short_link_unknown_prefix_falls_back_to_timeline_home(live_catalog):
    """A live catalog that holds sessions still answers "no match" for a stranger."""
    owner_id = live_catalog.create_user("owner@short-link.test")
    live_catalog.commit_session(owner_id=owner_id)

    resp = _client().get("/s/deadbeef")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/timeline"


def test_short_link_rejects_non_hex_prefix():
    """Below the resolver: a non-hex prefix never reaches the catalog at all."""
    resp = _client().get("/s/zzzznotahexid")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/timeline"
