"""HTTP-level tests for summary fields in session API responses.

Covers:
- GET /api/timeline/sessions returns the catalog's summary_title
- GET /api/timeline/sessions/{id} returns the same fields for one session
- Sessions without a title return null (not error)

The titles come from where production puts them: ``storage.session.title.
complete.v2`` against a real catalog, seeded through ``live_catalog_harness``.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import UUID

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _complete_title(live: LiveCatalog, session_id: UUID, title: str) -> None:
    """Give one session the AI title the title worker would have written."""

    completed = live.rpc(
        "storage.session.title.complete.v2",
        {
            "session_id": str(session_id),
            "title": title,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    assert completed["changed"] is True, completed


def _cookies(live: LiveCatalog, *, owner_id: int, email: str) -> dict[str, str]:
    return {"longhouse_session": live.browser_cookie(owner_id=owner_id, email=email)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_sessions_includes_summary(live_catalog, live_catalog_client):
    """The browser session list returns the session's title fields."""
    email = "summary-list@test.local"
    owner_id = live_catalog.create_user(email)
    seeded = live_catalog.commit_session(owner_id=owner_id)
    _complete_title(live_catalog, seeded.session_id, "Auth and Rate Limiting")

    resp = live_catalog_client.get("/timeline/sessions?days_back=1", cookies=_cookies(live_catalog, owner_id=owner_id, email=email))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["sessions"]) >= 1
    session = data["sessions"][0]["detail"]
    assert session["id"] == str(seeded.session_id)
    assert session["summary_title"] == "Auth and Rate Limiting"
    assert session["anchor_title"] == "Auth and Rate Limiting"
    assert session["environment"] == "production"
    assert session["origin_label"] == "production"
    # One session is its own thread: the lineage columns that made a session
    # point at another one are gone.
    assert session["thread_root_session_id"] == session["id"]
    assert session["thread_head_session_id"] == session["id"]
    assert session["thread_continuation_count"] == 1
    assert session["continuation_kind"] is None


def test_get_session_includes_summary(live_catalog, live_catalog_client):
    """The browser session detail returns the same title fields."""
    email = "summary-detail@test.local"
    owner_id = live_catalog.create_user(email)
    seeded = live_catalog.commit_session(owner_id=owner_id)
    _complete_title(live_catalog, seeded.session_id, "Database Bug Fix")

    resp = live_catalog_client.get(
        f"/timeline/sessions/{seeded.session_id}",
        cookies=_cookies(live_catalog, owner_id=owner_id, email=email),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["summary_title"] == "Database Bug Fix"
    assert data["anchor_title"] == "Database Bug Fix"
    assert data["environment"] == "production"
    assert data["origin_label"] == "production"
    assert data["thread_root_session_id"] == str(seeded.session_id)
    assert data["thread_head_session_id"] == str(seeded.session_id)
    assert data["thread_continuation_count"] == 1
    assert data["continuation_kind"] is None


def test_summary_null_when_missing(live_catalog, live_catalog_client):
    """Sessions with no AI title yet return the provisional one, never an error."""
    email = "summary-missing@test.local"
    owner_id = live_catalog.create_user(email)
    seeded = live_catalog.commit_session(owner_id=owner_id, texts=("hello from the transcript",))

    resp = live_catalog_client.get(
        f"/timeline/sessions/{seeded.session_id}",
        cookies=_cookies(live_catalog, owner_id=owner_id, email=email),
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["summary"] is None
    assert data["summary_status"] == "unavailable"
    # No AI title has landed, so the anchor is still empty and the session
    # renders under the prompt-derived placeholder.
    assert data["anchor_title"] is None
    assert data["title_state"] == "pending"
    assert data["title_source"] == "prompt"
    assert data["summary_title"] == "hello from the transcript"
