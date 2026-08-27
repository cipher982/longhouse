"""Tests for has_real_sessions flag in the sessions list response.

The flag exists so the web UI can tell "you have not shipped anything yet"
apart from "the demo corpus is all you are looking at". A Runtime Host answers
it out of catalogd (``LiveCatalogStore.list_sessions``), so these run against a
real live catalog rather than seeding archive rows.

Covers:
- has_real_sessions=False when every session came from device_id='demo-mac'
- has_real_sessions=True when at least one session has a different device_id
- has_real_sessions=True when there are no sessions at all
"""

import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401

DEMO_DEVICE_ID = "demo-mac"


def _owner(live_catalog) -> tuple[int, dict[str, str]]:  # noqa: F811
    owner_id = live_catalog.create_user("owner@real-sessions.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    return owner_id, {"X-Agents-Token": token}


def test_has_real_sessions_false_when_only_demo(live_catalog, live_catalog_client):  # noqa: F811
    """has_real_sessions=False when every session came from the demo machine."""
    owner_id, headers = _owner(live_catalog)
    live_catalog.commit_session(owner_id=owner_id, device_id=DEMO_DEVICE_ID)
    live_catalog.commit_session(owner_id=owner_id, device_id=DEMO_DEVICE_ID)

    response = live_catalog_client.get("/agents/sessions", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["has_real_sessions"] is False


def test_has_real_sessions_true_when_real_session_exists(live_catalog, live_catalog_client):  # noqa: F811
    """has_real_sessions=True when at least one non-demo session exists."""
    owner_id, headers = _owner(live_catalog)
    live_catalog.commit_session(owner_id=owner_id, device_id=DEMO_DEVICE_ID)
    live_catalog.commit_session(owner_id=owner_id, device_id="laptop-abc123")

    response = live_catalog_client.get("/agents/sessions", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["has_real_sessions"] is True


def test_has_real_sessions_true_when_no_sessions(live_catalog, live_catalog_client):  # noqa: F811
    """has_real_sessions=True on an empty corpus (default, avoids false banners)."""
    _owner_id, headers = _owner(live_catalog)

    response = live_catalog_client.get("/agents/sessions", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["has_real_sessions"] is True
