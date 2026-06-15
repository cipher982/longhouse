"""Tests for the anonymous /s/<prefix>/preview endpoint used by the login page.

The endpoint surfaces public-safe session metadata (provider, device label,
timing, owner display info) to logged-out visitors so the login page can tell
them whose session they were trying to reach. It must not leak transcript,
project, cwd, or any content-derived field.
"""

from __future__ import annotations

import os
import uuid as _uuid
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/lh_session_preview_test.db")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")

from fastapi.testclient import TestClient

from zerg.database import db_session
from zerg.database import initialize_database
from zerg.models.agents import AgentSession
from zerg.models.user import User

initialize_database()

from zerg.main import app


@pytest.fixture(autouse=True)
def _isolate_state():
    """Each test starts with empty users and sessions tables so prefix
    lookups stay deterministic across runs and the fixture doesn't accumulate
    rows that could collide with new tests' prefixes.
    """
    with db_session() as db:
        db.query(User).delete()
        db.query(AgentSession).delete()


def _seed_user(*, display_name: str | None = "David Rose", email: str = "david010@gmail.com") -> None:
    with db_session() as db:
        db.add(User(email=email, display_name=display_name, is_active=True))


def _seed_session(
    *,
    provider: str = "codex",
    device_name: str | None = "cinder",
    project: str = "cipher982/longhouse",
    cwd: str = "/Users/david/git/zerg/longhouse",
    summary_title: str = "Refactor session view",
) -> str:
    session_id = uuid4()
    with db_session() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider=provider,
                environment="production",
                device_name=device_name,
                project=project,
                cwd=cwd,
                summary_title=summary_title,
                first_user_message_preview="Could you refactor the session view?",
                started_at=datetime.now(timezone.utc),
            )
        )
    return str(session_id)


def test_preview_returns_public_safe_metadata():
    _seed_user()
    session_id = _seed_session()
    prefix = session_id.split("-")[0]
    client = TestClient(app)

    resp = client.get(f"/s/{prefix}/preview")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert body["provider"] == "codex"
    assert body["device_name"] == "cinder"
    assert body["started_at"] is not None
    assert body["ended_at"] is None
    assert body["owner_display_name"] == "David Rose"
    assert body["owner_email_local"] == "david010"
    # No content-derived fields must leak through.
    for forbidden in (
        "summary_title",
        "first_user_message_preview",
        "project",
        "cwd",
        "git_repo",
        "git_branch",
        "summary",
        "user_state",
        "device_id",
    ):
        assert forbidden not in body, f"{forbidden!r} leaked into preview response"
    # Public-cache header so the login page can refetch cheaply.
    assert "max-age" in resp.headers.get("cache-control", "")


def test_preview_falls_back_to_email_local_when_display_name_is_blank():
    with db_session() as db:
        db.add(User(email="david010@gmail.com", display_name=None, is_active=True))
        db.add(User(email="other@gmail.com", display_name="   ", is_active=True))
    session_id = _seed_session()
    prefix = session_id.split("-")[0]
    client = TestClient(app)

    resp = client.get(f"/s/{prefix}/preview")

    assert resp.status_code == 200
    body = resp.json()
    # First user by id is the one with display_name=None.
    assert body["owner_display_name"] is None
    assert body["owner_email_local"] == "david010"


def test_preview_404_on_unknown_prefix():
    client = TestClient(app)

    resp = client.get("/s/deadbeef/preview")

    assert resp.status_code == 404


def test_preview_404_on_invalid_prefix():
    client = TestClient(app)

    resp = client.get("/s/zzzznotahex/preview")

    assert resp.status_code == 404


def test_preview_404_when_no_sessions_match():
    _seed_user()
    client = TestClient(app)

    # 8 hex chars but no row in the DB.
    resp = client.get("/s/00000000/preview")

    assert resp.status_code == 404


def test_preview_does_not_require_auth():
    _seed_user()
    session_id = _seed_session()
    prefix = session_id.split("-")[0]
    # No cookies, no auth headers — endpoint must be public.
    client = TestClient(app)

    resp = client.get(f"/s/{prefix}/preview")

    assert resp.status_code == 200


def test_preview_404_on_ambiguous_prefix():
    """Two sessions whose ids share the same 8-char prefix must not let the
    preview endpoint guess which one the visitor meant.
    """
    _seed_user()
    same_prefix = "abcdef01"
    # Construct two UUIDs that share the 8-char prefix. Version/variant
    # nibbles don't matter — the column stores raw CHAR(36).
    id_a = _uuid.UUID(f"{same_prefix}-1234-1234-1234-123456789012")
    id_b = _uuid.UUID(f"{same_prefix}-1234-1234-1234-123456789013")
    with db_session() as db:
        for sid in (id_a, id_b):
            db.add(
                AgentSession(
                    id=sid,
                    provider="claude",
                    environment="production",
                    started_at=datetime.now(timezone.utc),
                )
            )
    client = TestClient(app)

    resp = client.get(f"/s/{same_prefix}/preview")

    assert resp.status_code == 404


def test_preview_works_when_no_user_is_configured():
    # No User row at all — owner fields must be null, not 500.
    session_id = _seed_session()
    prefix = session_id.split("-")[0]
    client = TestClient(app)

    resp = client.get(f"/s/{prefix}/preview")

    assert resp.status_code == 200
    body = resp.json()
    assert body["owner_display_name"] is None
    assert body["owner_email_local"] is None
