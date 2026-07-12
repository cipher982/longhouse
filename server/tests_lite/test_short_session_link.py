"""Tests for the /s/<prefix> short session-link redirect used by the CLI launch panel."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/lh_short_link_test.db")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")

from fastapi.testclient import TestClient

import zerg.database as database_module
from zerg.database import db_session
from zerg.database import initialize_database
from zerg.models.agents import AgentSession

initialize_database()

from zerg.main import app  # noqa: E402


def _seed_session() -> str:
    session_id = uuid4()
    with db_session() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="production",
                started_at=datetime.now(timezone.utc),
            )
        )
    return str(session_id)


def test_short_link_redirects_to_full_timeline_url():
    session_id = _seed_session()
    prefix = session_id.split("-")[0]
    client = TestClient(app, follow_redirects=False)

    resp = client.get(f"/s/{prefix}")

    assert resp.status_code == 302
    assert resp.headers["location"] == f"/timeline/{session_id}"


def test_short_link_unknown_prefix_falls_back_to_timeline_home():
    client = TestClient(app, follow_redirects=False)

    resp = client.get("/s/deadbeef")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/timeline"


def test_short_link_rejects_non_hex_prefix():
    client = TestClient(app, follow_redirects=False)

    resp = client.get("/s/zzzznotahexid")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/timeline"


def test_short_link_resolves_from_live_catalog_without_archive(tmp_path, monkeypatch):
    session_id = uuid4()
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        "zerg.services.catalog_read_gateway.resolve_session_prefix",
        lambda _prefix: {
            "status": "unique",
            "session": {"session_id": str(session_id)},
            "owner": None,
        },
    )

    response = TestClient(app, follow_redirects=False).get(f"/s/{str(session_id).split('-')[0]}")

    assert response.status_code == 302
    assert response.headers["location"] == f"/timeline/{session_id}"
