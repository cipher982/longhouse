"""Tests for device token API endpoints."""

import uuid

from fastapi.testclient import TestClient

from zerg.routers.device_tokens import generate_device_token
from zerg.routers.device_tokens import hash_token


class TestDeviceTokenHelpers:
    """Tests for device token helper functions."""

    def test_generate_device_token_format(self):
        """Generated tokens have correct format."""
        token = generate_device_token()

        assert token.startswith("zdt_")
        assert len(token) > 40  # zdt_ + base64 encoded bytes

    def test_generate_device_token_unique(self):
        """Each generated token is unique."""
        tokens = [generate_device_token() for _ in range(100)]

        assert len(set(tokens)) == 100

    def test_hash_token_deterministic(self):
        """Same token produces same hash."""
        token = "zdt_test_token"

        hash1 = hash_token(token)
        hash2 = hash_token(token)

        assert hash1 == hash2

    def test_hash_token_different_for_different_tokens(self):
        """Different tokens produce different hashes."""
        hash1 = hash_token("zdt_token_1")
        hash2 = hash_token("zdt_token_2")

        assert hash1 != hash2


class TestDeviceTokenAPI:
    """Tests for device token API endpoints."""

    def test_create_device_token(self, client: TestClient):
        """Create a new device token."""
        response = client.post(
            "/api/devices/tokens",
            json={"device_id": "test-laptop"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["device_id"] == "test-laptop"
        assert data["token"].startswith("zdt_")
        assert "id" in data
        assert "created_at" in data

    def test_create_device_token_empty_device_id(self, client: TestClient):
        """Reject empty device_id."""
        response = client.post(
            "/api/devices/tokens",
            json={"device_id": ""},
        )

        assert response.status_code == 422

    def test_list_device_tokens(self, client: TestClient):
        """List all device tokens."""
        # Create a token first
        client.post("/api/devices/tokens", json={"device_id": "device-1"})
        client.post("/api/devices/tokens", json={"device_id": "device-2"})

        response = client.get("/api/devices/tokens")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 2
        # Tokens should not include plain token
        for token in data["tokens"]:
            assert "token" not in token
            assert "id" in token
            assert "device_id" in token
            assert "is_valid" in token

    def test_list_excludes_revoked_by_default(self, client: TestClient, db_session):
        """Revoked tokens are excluded by default."""
        # Create and revoke a token
        create_resp = client.post("/api/devices/tokens", json={"device_id": "to-revoke"})
        token_id = create_resp.json()["id"]
        client.delete(f"/api/devices/tokens/{token_id}")

        # List without include_revoked
        response = client.get("/api/devices/tokens")
        data = response.json()

        token_ids = [t["id"] for t in data["tokens"]]
        assert token_id not in token_ids

    def test_list_includes_revoked_when_requested(self, client: TestClient):
        """Revoked tokens included when include_revoked=true."""
        # Create and revoke a token
        create_resp = client.post("/api/devices/tokens", json={"device_id": "to-revoke-2"})
        token_id = create_resp.json()["id"]
        client.delete(f"/api/devices/tokens/{token_id}")

        # List with include_revoked
        response = client.get("/api/devices/tokens", params={"include_revoked": True})
        data = response.json()

        token_ids = [t["id"] for t in data["tokens"]]
        assert token_id in token_ids

    def test_get_device_token(self, client: TestClient):
        """Get a specific device token."""
        create_resp = client.post("/api/devices/tokens", json={"device_id": "get-test"})
        token_id = create_resp.json()["id"]

        response = client.get(f"/api/devices/tokens/{token_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == token_id
        assert data["device_id"] == "get-test"
        assert data["is_valid"] is True
        assert "token" not in data

    def test_get_nonexistent_token(self, client: TestClient):
        """404 for nonexistent token."""
        fake_id = str(uuid.uuid4())
        response = client.get(f"/api/devices/tokens/{fake_id}")

        assert response.status_code == 404

    def test_revoke_device_token(self, client: TestClient):
        """Revoke a device token."""
        create_resp = client.post("/api/devices/tokens", json={"device_id": "revoke-test"})
        token_id = create_resp.json()["id"]

        response = client.delete(f"/api/devices/tokens/{token_id}")

        assert response.status_code == 204

        # Verify revoked
        get_resp = client.get(f"/api/devices/tokens/{token_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["is_valid"] is False
        assert get_resp.json()["revoked_at"] is not None

    def test_revoke_already_revoked(self, client: TestClient):
        """Error when revoking already revoked token."""
        create_resp = client.post("/api/devices/tokens", json={"device_id": "double-revoke"})
        token_id = create_resp.json()["id"]

        # First revoke succeeds
        client.delete(f"/api/devices/tokens/{token_id}")

        # Second revoke fails
        response = client.delete(f"/api/devices/tokens/{token_id}")
        assert response.status_code == 400

    def test_revoke_nonexistent_token(self, client: TestClient):
        """404 when revoking nonexistent token."""
        fake_id = str(uuid.uuid4())
        response = client.delete(f"/api/devices/tokens/{fake_id}")

        assert response.status_code == 404


class TestDeviceTokenValidation:
    """Tests for device token validation in agents API."""

    def test_valid_device_token_allows_access(self, client: TestClient, monkeypatch):
        """Valid device token grants access to agents API."""
        # Patch the module-level _settings to disable auth bypass
        import zerg.routers.agents as agents_mod

        monkeypatch.setattr(agents_mod._settings, "auth_disabled", False)

        # Create a device token
        create_resp = client.post("/api/devices/tokens", json={"device_id": "test-device"})
        token = create_resp.json()["token"]

        # Use it to access agents API
        # We're testing that auth passes - the endpoint may fail for other reasons (no postgres)
        # so we check for NOT 401/403
        response = client.get(
            "/api/agents/sessions",
            headers={"X-Agents-Token": token},
        )

        # Accept 200 (success) or 501 (postgres not available) but NOT 401/403
        assert response.status_code in (200, 501)

    def test_revoked_token_denied(self, client: TestClient, monkeypatch):
        """Revoked device token is denied."""
        import zerg.routers.agents as agents_mod

        monkeypatch.setattr(agents_mod._settings, "auth_disabled", False)

        # Create and revoke a token
        create_resp = client.post("/api/devices/tokens", json={"device_id": "revoked-device"})
        token = create_resp.json()["token"]
        token_id = create_resp.json()["id"]
        client.delete(f"/api/devices/tokens/{token_id}")

        # Try to use it
        response = client.get(
            "/api/agents/sessions",
            headers={"X-Agents-Token": token},
        )

        assert response.status_code == 401

    def test_invalid_token_denied(self, client: TestClient, monkeypatch):
        """Invalid device token is denied."""
        import zerg.routers.agents as agents_mod

        monkeypatch.setattr(agents_mod._settings, "auth_disabled", False)
        # Clear legacy token
        monkeypatch.setattr(agents_mod._settings, "agents_api_token", None)

        response = client.get(
            "/api/agents/sessions",
            headers={"X-Agents-Token": "zdt_invalid_token"},
        )

        assert response.status_code == 401
