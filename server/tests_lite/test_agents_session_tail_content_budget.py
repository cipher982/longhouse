"""HTTP-level tests for the per-event content budget on GET /api/agents/sessions/{id}/tail.

Why this exists: tail used to slice content at a bare 4000 chars. A real
overnight session's closing message was cut mid-word at exactly that boundary
with nothing in the response to say so, and an agent reading the tail concluded
the archive had lost the text. The archive was intact. Truncation must be
visible and the caller must be able to ask for the rest.

Tail reads storage-v2 render objects through the live catalog, so the session
here is shipped through the real envelope route into a real catalog rather than
inserted as archive rows behind a ``get_db`` override.
"""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401

DEVICE_ID = "cinder"

# The real message was 4345 chars and the cut landed at 4000, losing 345.
_LONG_TAIL = "and a real question I don't have a confident answer to: should dispatching a new turn clear unread"
_LONG_CONTENT = ("x" * 4000) + _LONG_TAIL


def _ship_session_with_long_final_message(live_catalog: LiveCatalog, client, *, owner_id: int, headers: dict[str, str]) -> UUID:
    """A shipped transcript whose closing assistant message is over budget."""

    session_id = uuid4()
    body = live_catalog.envelope_body(
        session_id=session_id,
        device_id=DEVICE_ID,
        texts=("short prompt", _LONG_CONTENT),
    )
    body["render"]["records"][1]["role"] = "assistant"
    response = client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={**headers, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    return session_id


def _owner_headers(live_catalog: LiveCatalog) -> tuple[int, dict[str, str]]:
    owner_id = live_catalog.create_user("owner@tail-budget.test")
    return owner_id, {"X-Agents-Token": live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)}


def test_over_budget_content_is_annotated_not_silently_cut(live_catalog, live_catalog_client):
    owner_id, headers = _owner_headers(live_catalog)
    session_id = _ship_session_with_long_final_message(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers)

    resp = live_catalog_client.get(f"/agents/sessions/{session_id}/tail", params={"limit": 10}, headers=headers)

    assert resp.status_code == 200, resp.text
    final = resp.json()["events"][-1]
    assert len(final["content"]) == 4000
    assert final["_content_truncated"] is True
    assert final["_content_full_chars"] == len(_LONG_CONTENT)


def test_raising_the_budget_returns_the_rest(live_catalog, live_catalog_client):
    """The annotation promises the caller can re-request. It has to be true."""
    owner_id, headers = _owner_headers(live_catalog)
    session_id = _ship_session_with_long_final_message(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers)

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        params={"limit": 10, "max_content_chars": 20000},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    final = resp.json()["events"][-1]
    assert final["content"] == _LONG_CONTENT
    assert final["content"].endswith("clear unread")
    assert "_content_truncated" not in final


def test_under_budget_content_carries_no_annotation(live_catalog, live_catalog_client):
    owner_id, headers = _owner_headers(live_catalog)
    session_id = _ship_session_with_long_final_message(live_catalog, live_catalog_client, owner_id=owner_id, headers=headers)

    resp = live_catalog_client.get(f"/agents/sessions/{session_id}/tail", params={"limit": 10}, headers=headers)

    assert resp.status_code == 200, resp.text
    first = resp.json()["events"][0]
    assert first["content"] == "short prompt"
    assert "_content_truncated" not in first
    assert "_content_full_chars" not in first
