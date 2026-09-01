"""The session tail role filter applies before its result limit."""

from __future__ import annotations

import os
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401, F811
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401, F811

DEVICE_ID = "cinder"
BURIED_TURN = "the tablet is already paired for wireless adb"


def _owner_headers(live_catalog) -> dict[str, str]:  # noqa: F811
    owner_id = live_catalog.create_user("owner@tail-roles.test")
    return {"X-Agents-Token": live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)}


def _ship_tool_heavy_session(live_catalog, client, headers):  # noqa: F811
    session_id = uuid4()
    texts = (BURIED_TURN,) + tuple(f"Script completed {index}" for index in range(60))
    body = live_catalog.envelope_body(session_id=session_id, device_id=DEVICE_ID, texts=texts)
    for record in body["render"]["records"][1:]:
        record["role"] = "tool"
        record["tool_name"] = "Bash"
    response = client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={**headers, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    return session_id


def test_tail_without_roles_returns_tool_spam(live_catalog, live_catalog_client):  # noqa: F811
    headers = _owner_headers(live_catalog)
    session_id = _ship_tool_heavy_session(live_catalog, live_catalog_client, headers)

    response = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        params={"limit": 10},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert {event["role"] for event in response.json()["events"]} == {"tool"}


def test_tail_roles_filter_surfaces_buried_turn_before_limit(live_catalog, live_catalog_client):  # noqa: F811
    headers = _owner_headers(live_catalog)
    session_id = _ship_tool_heavy_session(live_catalog, live_catalog_client, headers)

    response = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        params={"limit": 3, "roles": "user,assistant"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [event["role"] for event in payload["events"]] == ["user"]
    assert "wireless adb" in payload["events"][0]["content"]
    assert payload["window_exhausted"] is False
