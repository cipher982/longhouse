"""Tests for the `/api/users/me` profile endpoints."""

from unittest.mock import AsyncMock
from unittest.mock import patch

from fastapi.testclient import TestClient


def _assert_user_payload(payload: dict):
    """Basic sanity checks for a UserOut JSON dict."""

    assert {
        "id",
        "email",
        "is_active",
        "created_at",
        "display_name",
        "avatar_url",
        "prefs",
        "last_login",
    }.issubset(payload.keys())


def test_get_current_user(client: TestClient):
    """GET /api/users/me returns the current profile even when auth is disabled."""

    resp = client.get("/api/users/me")
    assert resp.status_code == 200
    data = resp.json()

    _assert_user_payload(data)

    # In dev-mode the stub account is `dev@local`.
    assert data["email"] == "dev@local"
    # Fresh dev user has no display name by default.
    assert data["display_name"] is None


def test_update_current_user_display_name(client: TestClient):
    """PUT /api/users/me updates only the provided fields (partial patch)."""

    # 1. Patch the display_name and avatar_url
    patch_body = {
        "display_name": "Alice",
        "avatar_url": "https://cdn.example.com/avatars/alice.png",
        "prefs": {"theme": "dark"},
    }

    resp = client.put("/api/users/me", json=patch_body)
    assert resp.status_code == 200
    updated = resp.json()
    _assert_user_payload(updated)

    assert updated["display_name"] == patch_body["display_name"]
    assert updated["avatar_url"] == patch_body["avatar_url"]
    assert updated["prefs"] == patch_body["prefs"]

    # 2. GET again – fields should persist
    resp2 = client.get("/api/users/me")
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["display_name"] == patch_body["display_name"]
    assert data["avatar_url"] == patch_body["avatar_url"]
    assert data["prefs"] == patch_body["prefs"]


def test_user_updated_event_emitted(client: TestClient):
    """PUT /api/users/me publishes a USER_UPDATED event via the event bus."""

    from zerg.events import event_bus

    # AsyncMock to capture calls
    async_mock = AsyncMock()

    with patch.object(event_bus, "publish", new=async_mock) as mock_publish:
        body = {"display_name": "Bob"}
        resp = client.put("/api/users/me", json=body)
        assert resp.status_code == 200

        # publish should have been awaited once with EventType.USER_UPDATED
        assert mock_publish.await_count == 1
        call_obj = mock_publish.await_args

        # First positional arg is the EventType
        event_type = call_obj.args[0]
        # Payload dict is second arg
        payload = call_obj.args[1]

        from zerg.events import EventType

        assert event_type == EventType.USER_UPDATED
        # The payload must include id and display_name
        assert "id" in payload
        assert payload["display_name"] == "Bob"
