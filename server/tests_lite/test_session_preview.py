"""Tests for the owner-authorized /s/<prefix>/preview endpoint.

The endpoint surfaces a small metadata shape after browser authentication. It
must not leak another owner's session through prefix enumeration, nor expose
transcript, project, cwd, or any content-derived field.

The route resolves the prefix through catalogd, so every test that expects a
match provisions a real live catalog and seeds the session in it. The prefix
rules are the interesting part: below eight hex characters, and on a zero or
ambiguous match, the route must refuse rather than guess or confirm existence.
"""

from __future__ import annotations

import os
import uuid as _uuid
from datetime import UTC
from datetime import datetime
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")

from fastapi.testclient import TestClient  # noqa: E402

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402,F401
from zerg.main import app  # noqa: E402

# The preview route hangs off ``app`` rather than ``api_app`` and takes no
# database dependency at all, so it needs the catalog the fixture provisions but
# not the fixture's request client. Constructing the client without entering it
# keeps the app's lifespan out of the way.


def _seed_user(live: LiveCatalog, *, display_name: str | None = "David Rose", email: str = "david010@example.com") -> int:
    owner_id = live.create_user(email)
    if display_name is not None:
        live.rpc(
            "auth.user.update.v2",
            {
                "user_id": owner_id,
                "display_name": display_name,
                "avatar_url": None,
                "prefs": None,
                "update_mask": ["display_name"],
            },
        )
    return owner_id


def _cookies(live: LiveCatalog, owner_id: int, *, email: str = "david010@example.com") -> dict[str, str]:
    return {"longhouse_session": live.browser_cookie(owner_id=owner_id, email=email)}


def _seed_session(
    live: LiveCatalog,
    *,
    owner_id: int,
    session_id: _uuid.UUID | None = None,
    provider: str = "codex",
    device_id: str = "cinder",
) -> str:
    """One Console session in the live catalog, carrying no content fields."""

    session_id = session_id or uuid4()
    live.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(uuid4()),
                "owner_id": owner_id,
                "provider": provider,
                "device_id": device_id,
                "cwd": "/Users/david/git/zerg/longhouse",
                "project": "cipher982/longhouse",
                "display_name": "Refactor session view",
                "started_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    return str(session_id)


def test_preview_returns_public_safe_metadata(live_catalog):  # noqa: F811
    owner_id = _seed_user(live_catalog)
    session_id = _seed_session(live_catalog, owner_id=owner_id)
    prefix = session_id.split("-")[0]

    resp = TestClient(app).get(f"/s/{prefix}/preview", cookies=_cookies(live_catalog, owner_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == session_id
    assert body["provider"] == "codex"
    assert body["device_name"] == "cinder"
    assert body["started_at"] is not None
    assert body["ended_at"] is None
    assert body["owner_display_name"] == "David Rose"
    assert "owner_email_local" not in body
    # No content-derived fields must leak through. catalogd resolves several of
    # them for the session (project, cwd, summary title); the route is what
    # must not carry them out to an unauthenticated visitor.
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
    # Never shared-cacheable: this is per-session metadata.
    assert resp.headers.get("cache-control") == "private, no-store"


def test_preview_falls_back_to_email_local_when_display_name_is_blank(live_catalog):  # noqa: F811
    owner_id = _seed_user(live_catalog, display_name=None)
    session_id = _seed_session(live_catalog, owner_id=owner_id)
    prefix = session_id.split("-")[0]

    resp = TestClient(app).get(f"/s/{prefix}/preview", cookies=_cookies(live_catalog, owner_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["owner_display_name"] is None
    # This route is unauthenticated. A blank display name must degrade to
    # nothing, not to the email local-part -- that turned the preview into
    # anonymous de-anonymization of the instance owner. catalogd still resolves
    # ``email_local`` in its answer, so the guard is the route dropping it.
    assert "owner_email_local" not in body
    assert "david010" not in resp.text


def test_preview_404_on_unknown_prefix(live_catalog):  # noqa: F811
    owner_id = _seed_user(live_catalog)
    resp = TestClient(app).get("/s/deadbeef/preview", cookies=_cookies(live_catalog, owner_id))

    assert resp.status_code == 404


def test_preview_404_on_invalid_prefix(live_catalog):  # noqa: F811
    # Refused on shape alone, before any catalog read: a short or non-hex
    # prefix stops being a link and starts being instance enumeration.
    owner_id = _seed_user(live_catalog)
    resp = TestClient(app).get("/s/zzzznotahex/preview", cookies=_cookies(live_catalog, owner_id))

    assert resp.status_code == 404


def test_preview_404_when_no_sessions_match(live_catalog):  # noqa: F811
    owner_id = _seed_user(live_catalog)

    # 8 hex chars but no session in the catalog.
    resp = TestClient(app).get("/s/00000000/preview", cookies=_cookies(live_catalog, owner_id))

    assert resp.status_code == 404


def test_preview_requires_auth(live_catalog):  # noqa: F811
    owner_id = _seed_user(live_catalog)
    session_id = _seed_session(live_catalog, owner_id=owner_id)
    prefix = session_id.split("-")[0]

    resp = TestClient(app).get(f"/s/{prefix}/preview")

    assert resp.status_code == 401, resp.text


def test_preview_404_on_ambiguous_prefix(live_catalog):  # noqa: F811
    """Two sessions whose ids share the same 8-char prefix must not let the
    preview endpoint guess which one the visitor meant.
    """
    owner_id = _seed_user(live_catalog)
    same_prefix = "abcdef01"
    # Construct two UUIDs that share the 8-char prefix. Version/variant
    # nibbles don't matter -- the column stores raw CHAR(36).
    for suffix in ("123456789012", "123456789013"):
        _seed_session(
            live_catalog,
            owner_id=owner_id,
            session_id=_uuid.UUID(f"{same_prefix}-1234-1234-1234-{suffix}"),
            provider="claude",
        )

    resp = TestClient(app).get(f"/s/{same_prefix}/preview", cookies=_cookies(live_catalog, owner_id))

    assert resp.status_code == 404


def test_preview_hides_session_owned_by_another_identity(live_catalog):  # noqa: F811
    owner_id = _seed_user(live_catalog)
    session_id = _seed_session(live_catalog, owner_id=999_999, device_id="ghost")
    prefix = session_id.split("-")[0]

    resp = TestClient(app).get(f"/s/{prefix}/preview", cookies=_cookies(live_catalog, owner_id))

    assert resp.status_code == 404, resp.text
