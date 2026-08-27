"""Query-less recent-sessions listing on GET /agents/sessions.

The MCP search_sessions tool forwards here. Omitting the query (or sending a
blank one) must list recent sessions ordered by last activity, honoring the
project/provider/days_back/limit filters, with no match snippet or score.
A real query must keep the existing content-search behavior.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401

OWNER_EMAIL = "owner@queryless.test"
DEVICE_ID = "cinder"


def _owner(live_catalog) -> tuple[int, dict[str, str]]:
    owner_id = live_catalog.create_user(OWNER_EMAIL)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)
    return owner_id, {"X-Agents-Token": token}


def test_queryless_listing_returns_recent_sessions_ordered_by_activity(live_catalog, live_catalog_client):
    owner_id, headers = _owner(live_catalog)
    now = datetime.now(UTC).replace(microsecond=0)

    older = live_catalog.commit_session(owner_id=owner_id, texts=("older",), now=now - timedelta(days=2))
    newest = live_catalog.commit_session(owner_id=owner_id, texts=("newest",), now=now - timedelta(hours=1))
    middle = live_catalog.commit_session(owner_id=owner_id, texts=("middle",), now=now - timedelta(days=1))

    response = live_catalog_client.get("/agents/sessions", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 3
    assert [s["id"] for s in payload["sessions"]] == [
        str(newest.session_id),
        str(middle.session_id),
        str(older.session_id),
    ]
    for session in payload["sessions"]:
        assert session["match_snippet"] is None
        assert session["match_score"] is None


def test_blank_query_is_treated_as_absent(live_catalog, live_catalog_client):
    owner_id, headers = _owner(live_catalog)
    now = datetime.now(UTC).replace(microsecond=0)

    older = live_catalog.commit_session(owner_id=owner_id, texts=("older",), now=now - timedelta(days=1))
    newer = live_catalog.commit_session(owner_id=owner_id, texts=("newer",), now=now - timedelta(hours=1))

    for blank in ("", "   "):
        response = live_catalog_client.get("/agents/sessions", params={"query": blank}, headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        # An FTS search for "" would return zero sessions; the listing
        # path returns the full recent-ordered corpus.
        assert payload["total"] == 2
        assert [s["id"] for s in payload["sessions"]] == [str(newer.session_id), str(older.session_id)]
        assert all(s["match_snippet"] is None for s in payload["sessions"])


def test_queryless_listing_honors_filters(live_catalog, live_catalog_client):
    owner_id, headers = _owner(live_catalog)
    now = datetime.now(UTC).replace(microsecond=0)

    zerg_recent = live_catalog.commit_session(owner_id=owner_id, project="zerg", now=now - timedelta(hours=1))
    zerg_older = live_catalog.commit_session(owner_id=owner_id, project="zerg", now=now - timedelta(hours=2))
    other_project = live_catalog.commit_session(owner_id=owner_id, project="g55", now=now - timedelta(hours=3))
    stale = live_catalog.commit_session(owner_id=owner_id, project="zerg", now=now - timedelta(days=30))

    response = live_catalog_client.get("/agents/sessions", params={"project": "zerg"}, headers=headers)
    assert response.status_code == 200, response.text
    ids = [s["id"] for s in response.json()["sessions"]]
    assert ids == [str(zerg_recent.session_id), str(zerg_older.session_id)]
    assert str(other_project.session_id) not in ids
    assert str(stale.session_id) not in ids  # outside default days_back=14

    response = live_catalog_client.get("/agents/sessions", params={"provider": "codex"}, headers=headers)
    assert [s["id"] for s in response.json()["sessions"]] == [
        str(zerg_recent.session_id),
        str(zerg_older.session_id),
        str(other_project.session_id),
    ]

    response = live_catalog_client.get("/agents/sessions", params={"provider": "claude"}, headers=headers)
    assert response.json()["sessions"] == []

    response = live_catalog_client.get("/agents/sessions", params={"days_back": 60, "project": "zerg"}, headers=headers)
    assert [s["id"] for s in response.json()["sessions"]] == [
        str(zerg_recent.session_id),
        str(zerg_older.session_id),
        str(stale.session_id),
    ]

    response = live_catalog_client.get("/agents/sessions", params={"limit": 2}, headers=headers)
    payload = response.json()
    assert [s["id"] for s in payload["sessions"]] == [str(zerg_recent.session_id), str(zerg_older.session_id)]
    assert payload["total"] == 3


def test_query_search_behavior_unchanged(live_catalog, live_catalog_client):
    owner_id, headers = _owner(live_catalog)
    now = datetime.now(UTC).replace(microsecond=0)

    match = live_catalog.commit_session(
        owner_id=owner_id,
        texts=("rotating refresh tokens shipped",),
        now=now - timedelta(days=1),
    )
    live_catalog.commit_session(
        owner_id=owner_id,
        texts=("unrelated timeline work",),
        now=now - timedelta(hours=1),
    )
    assert live_catalog.index_search() == 2

    response = live_catalog_client.get("/agents/sessions", params={"query": "refresh tokens"}, headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [s["id"] for s in payload["sessions"]] == [str(match.session_id)]
    assert payload["sessions"][0]["match_snippet"]
    assert "refresh" in payload["sessions"][0]["match_snippet"].lower()
