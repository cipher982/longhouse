"""Events API paging and projection-mode validation over storage-v2."""

import os
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401,E402
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401,E402


def test_events_api_anchor_tail_returns_latest_window(live_catalog, live_catalog_client):
    """`anchor=tail` pages the end of a transcript and reports the full total."""

    owner_id = live_catalog.create_user("events-branch-mode@example.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id = uuid4()
    live_catalog.commit_session(
        owner_id=owner_id,
        session_id=session_id,
        texts=tuple(f"event {index}" for index in range(1, 6)),
    )

    response = live_catalog_client.get(
        f"/agents/sessions/{session_id}/events",
        params={"limit": 2, "anchor": "tail"},
        headers={"X-Agents-Token": token},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 5
    assert [row["content_text"] for row in data["events"]] == ["event 4", "event 5"]

    bad_anchor = live_catalog_client.get(
        f"/agents/sessions/{session_id}/events",
        params={"anchor": "middle"},
        headers={"X-Agents-Token": token},
    )
    assert bad_anchor.status_code == 400
    assert "anchor" in bad_anchor.json()["detail"]

    bad_branch_mode = live_catalog_client.get(
        f"/agents/sessions/{session_id}/events",
        params={"branch_mode": "bad"},
        headers={"X-Agents-Token": token},
    )
    assert bad_branch_mode.status_code == 400
    assert "branch_mode" in bad_branch_mode.json()["detail"]


def test_events_api_rejects_offset_pagination(live_catalog, live_catalog_client):
    """Storage-v2 pages by cursor; an offset request is refused rather than ignored."""

    owner_id = live_catalog.create_user("events-offset@example.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id = uuid4()
    live_catalog.commit_session(
        owner_id=owner_id,
        session_id=session_id,
        texts=tuple(f"event {index}" for index in range(1, 6)),
    )

    response = live_catalog_client.get(
        f"/agents/sessions/{session_id}/events",
        params={"limit": 2, "anchor": "tail", "offset": 2},
        headers={"X-Agents-Token": token},
    )
    assert response.status_code == 400
    assert "cursor" in response.json()["detail"]
