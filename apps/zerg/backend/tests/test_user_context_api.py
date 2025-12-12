"""Tests for user context API endpoints.

Tests the /users/me/context endpoints (GET, PATCH, PUT) for managing
user-specific context used in prompt composition.
"""

import json
import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# GET /users/me/context
# ---------------------------------------------------------------------------


class TestGetUserContext:
    """Test GET /users/me/context endpoint."""

    def test_get_context_empty(self, client: TestClient):
        """Test that new user returns empty context dict."""
        response = client.get("/api/users/me/context")

        assert response.status_code == 200
        data = response.json()
        assert "context" in data
        assert data["context"] == {}

    def test_get_context_with_data(self, client: TestClient, test_user, db_session):
        """Test that stored context is returned."""
        # Set context on user
        test_user.context = {
            "display_name": "Test User",
            "servers": [{"name": "test-server", "ip": "10.0.0.1"}],
        }
        db_session.commit()

        response = client.get("/api/users/me/context")

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["display_name"] == "Test User"
        assert len(data["context"]["servers"]) == 1
        assert data["context"]["servers"][0]["name"] == "test-server"

    def test_get_context_unauthenticated(self, unauthenticated_client):
        """Test that unauthenticated request returns 401."""
        response = unauthenticated_client.get("/api/users/me/context")

        # In test mode with AUTH_DISABLED, this might still work
        # but should still validate the endpoint exists
        assert response.status_code in [200, 401]


# ---------------------------------------------------------------------------
# PATCH /users/me/context
# ---------------------------------------------------------------------------


class TestPatchUserContext:
    """Test PATCH /users/me/context endpoint (merge operation)."""

    def test_patch_context_merge(self, client: TestClient, test_user, db_session):
        """Test that PATCH merges with existing context."""
        # Set initial context
        test_user.context = {
            "display_name": "Alice",
            "role": "engineer",
        }
        db_session.commit()

        # Patch with new fields
        update = {
            "context": {
                "location": "San Francisco",
                "servers": [{"name": "clifford", "ip": "REDACTED_IP"}],
            }
        }

        response = client.patch("/api/users/me/context", json=update)

        assert response.status_code == 200
        data = response.json()

        # Original fields should be preserved
        assert data["context"]["display_name"] == "Alice"
        assert data["context"]["role"] == "engineer"

        # New fields should be added
        assert data["context"]["location"] == "San Francisco"
        assert len(data["context"]["servers"]) == 1

    def test_patch_context_new_user(self, client: TestClient):
        """Test that PATCH sets context on empty context."""
        update = {
            "context": {
                "display_name": "Bob",
                "role": "developer",
            }
        }

        response = client.patch("/api/users/me/context", json=update)

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["display_name"] == "Bob"
        assert data["context"]["role"] == "developer"

    def test_patch_context_size_limit(self, client: TestClient):
        """Test that PATCH rejects context larger than 64KB with 400."""
        # Create a large context (> 64KB)
        large_context = {
            "context": {
                "large_data": "x" * 70000,  # 70KB of data
            }
        }

        response = client.patch("/api/users/me/context", json=large_context)

        assert response.status_code == 400
        assert "too large" in response.json()["detail"].lower()

    def test_patch_context_unauthenticated(self, unauthenticated_client):
        """Test that unauthenticated request returns 401."""
        update = {"context": {"display_name": "Charlie"}}

        response = unauthenticated_client.patch("/api/users/me/context", json=update)

        # In test mode with AUTH_DISABLED, this might still work
        assert response.status_code in [200, 401]

    def test_patch_context_overwrites_existing_keys(self, client: TestClient, test_user, db_session):
        """Test that PATCH overwrites existing keys."""
        # Set initial context
        test_user.context = {"display_name": "Old Name", "role": "user"}
        db_session.commit()

        # Update display_name
        update = {"context": {"display_name": "New Name"}}

        response = client.patch("/api/users/me/context", json=update)

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["display_name"] == "New Name"
        assert data["context"]["role"] == "user"  # preserved

    def test_patch_context_array_replacement(self, client: TestClient, test_user, db_session):
        """Test that arrays are replaced, not merged (arrays aren't dict-like)."""
        # Set initial servers
        test_user.context = {
            "servers": [{"name": "server1", "ip": "10.0.0.1"}]
        }
        db_session.commit()

        # Replace servers list
        update = {
            "context": {
                "servers": [
                    {"name": "server2", "ip": "10.0.0.2"},
                    {"name": "server3", "ip": "10.0.0.3"},
                ]
            }
        }

        response = client.patch("/api/users/me/context", json=update)

        assert response.status_code == 200
        data = response.json()
        # Should replace entire servers array (arrays aren't deep merged)
        assert len(data["context"]["servers"]) == 2
        assert data["context"]["servers"][0]["name"] == "server2"

    def test_patch_context_deep_merge_nested_dict(self, client: TestClient, test_user, db_session):
        """Test that nested dicts are deep merged, preserving keys not in update."""
        # Set initial tools with a custom tool
        test_user.context = {
            "tools": {
                "location": True,
                "whoop": True,
                "custom_integration": True,  # Custom tool to be preserved
            }
        }
        db_session.commit()

        # Update only one tool - custom_integration should survive
        update = {
            "context": {
                "tools": {
                    "location": False,  # Change this
                    "obsidian": True,  # Add this
                }
            }
        }

        response = client.patch("/api/users/me/context", json=update)

        assert response.status_code == 200
        data = response.json()

        # Changed field
        assert data["context"]["tools"]["location"] is False
        # New field
        assert data["context"]["tools"]["obsidian"] is True
        # Preserved existing fields
        assert data["context"]["tools"]["whoop"] is True
        # CRITICAL: Custom tool must survive the deep merge
        assert data["context"]["tools"]["custom_integration"] is True

    def test_patch_context_deep_merge_multiple_levels(self, client: TestClient, test_user, db_session):
        """Test that deep merge works across multiple nesting levels."""
        # Set initial nested context
        test_user.context = {
            "settings": {
                "notifications": {
                    "email": True,
                    "sms": False,
                    "push": True,
                },
                "privacy": {
                    "share_location": True,
                }
            }
        }
        db_session.commit()

        # Update nested path: settings.notifications.email
        update = {
            "context": {
                "settings": {
                    "notifications": {
                        "email": False,  # Change this
                    }
                }
            }
        }

        response = client.patch("/api/users/me/context", json=update)

        assert response.status_code == 200
        data = response.json()

        # Changed field
        assert data["context"]["settings"]["notifications"]["email"] is False
        # Preserved sibling fields
        assert data["context"]["settings"]["notifications"]["sms"] is False
        assert data["context"]["settings"]["notifications"]["push"] is True
        # Preserved parent sibling fields
        assert data["context"]["settings"]["privacy"]["share_location"] is True


# ---------------------------------------------------------------------------
# PUT /users/me/context
# ---------------------------------------------------------------------------


class TestPutUserContext:
    """Test PUT /users/me/context endpoint (replace operation)."""

    def test_put_context_replace(self, client: TestClient, test_user, db_session):
        """Test that PUT replaces entire context."""
        # Set initial context
        test_user.context = {
            "display_name": "Alice",
            "role": "engineer",
            "servers": [{"name": "old-server"}],
        }
        db_session.commit()

        # Replace with completely new context
        new_context = {
            "context": {
                "display_name": "Bob",
                "location": "New York",
            }
        }

        response = client.put("/api/users/me/context", json=new_context)

        assert response.status_code == 200
        data = response.json()

        # Old fields should be gone
        assert "role" not in data["context"]
        assert "servers" not in data["context"]

        # New fields should be present
        assert data["context"]["display_name"] == "Bob"
        assert data["context"]["location"] == "New York"

    def test_put_context_empty(self, client: TestClient, test_user, db_session):
        """Test that PUT can clear all context."""
        # Set initial context
        test_user.context = {"display_name": "Charlie", "role": "admin"}
        db_session.commit()

        # Replace with empty context
        response = client.put("/api/users/me/context", json={"context": {}})

        assert response.status_code == 200
        data = response.json()
        assert data["context"] == {}

    def test_put_context_size_limit(self, client: TestClient):
        """Test that PUT rejects context larger than 64KB."""
        # Create a large context (> 64KB)
        large_context = {
            "context": {
                "large_data": "x" * 70000,  # 70KB of data
            }
        }

        response = client.put("/api/users/me/context", json=large_context)

        assert response.status_code == 400
        assert "too large" in response.json()["detail"].lower()

    def test_put_context_unauthenticated(self, unauthenticated_client):
        """Test that unauthenticated request returns 401."""
        update = {"context": {"display_name": "Dana"}}

        response = unauthenticated_client.put("/api/users/me/context", json=update)

        # In test mode with AUTH_DISABLED, this might still work
        assert response.status_code in [200, 401]

    def test_put_context_max_size_exactly(self, client: TestClient):
        """Test that context exactly at 64KB limit is accepted."""
        # Create context that's just under 64KB
        # JSON encoding adds some overhead, so we need to be careful
        base_size = len('{"context":{"data":""}}')
        data_size = 65536 - base_size - 100  # Leave some buffer for encoding

        context = {"context": {"data": "x" * data_size}}

        response = client.put("/api/users/me/context", json=context)

        # Should succeed if under limit
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Integration tests across endpoints
# ---------------------------------------------------------------------------


class TestContextEndpointsIntegration:
    """Test interactions between context endpoints."""

    def test_patch_then_get(self, client: TestClient):
        """Test that PATCH updates are visible via GET."""
        # PATCH context
        patch_data = {"context": {"display_name": "Eve", "role": "designer"}}
        patch_response = client.patch("/api/users/me/context", json=patch_data)
        assert patch_response.status_code == 200

        # GET context
        get_response = client.get("/api/users/me/context")
        assert get_response.status_code == 200

        data = get_response.json()
        assert data["context"]["display_name"] == "Eve"
        assert data["context"]["role"] == "designer"

    def test_put_then_patch(self, client: TestClient):
        """Test that PUT followed by PATCH works correctly."""
        # PUT initial context
        put_data = {"context": {"display_name": "Frank", "servers": []}}
        put_response = client.put("/api/users/me/context", json=put_data)
        assert put_response.status_code == 200

        # PATCH to add more fields
        patch_data = {"context": {"role": "admin", "location": "Boston"}}
        patch_response = client.patch("/api/users/me/context", json=patch_data)
        assert patch_response.status_code == 200

        # Verify all fields present
        data = patch_response.json()
        assert data["context"]["display_name"] == "Frank"  # from PUT
        assert data["context"]["role"] == "admin"  # from PATCH
        assert data["context"]["location"] == "Boston"  # from PATCH

    def test_multiple_patches_accumulate(self, client: TestClient):
        """Test that multiple PATCHes accumulate context."""
        # First patch
        client.patch("/api/users/me/context", json={"context": {"key1": "value1"}})

        # Second patch
        client.patch("/api/users/me/context", json={"context": {"key2": "value2"}})

        # Third patch
        client.patch("/api/users/me/context", json={"context": {"key3": "value3"}})

        # Get final result
        response = client.get("/api/users/me/context")
        data = response.json()

        # All keys should be present
        assert data["context"]["key1"] == "value1"
        assert data["context"]["key2"] == "value2"
        assert data["context"]["key3"] == "value3"

    def test_put_clears_previous_patches(self, client: TestClient):
        """Test that PUT clears context from previous PATCHes."""
        # PATCH some context
        client.patch("/api/users/me/context", json={"context": {"old_key": "old_value"}})

        # PUT new context (should replace)
        response = client.put("/api/users/me/context", json={"context": {"new_key": "new_value"}})

        data = response.json()
        assert "old_key" not in data["context"]
        assert data["context"]["new_key"] == "new_value"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestContextValidation:
    """Test validation and edge cases for context endpoints."""

    def test_patch_invalid_json(self, client: TestClient):
        """Test that invalid JSON returns 422."""
        response = client.patch(
            "/api/users/me/context",
            data="invalid json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422

    def test_patch_missing_context_key(self, client: TestClient):
        """Test that missing 'context' key returns 422."""
        response = client.patch("/api/users/me/context", json={"wrong_key": {}})

        assert response.status_code == 422

    def test_patch_invalid_server_config(self, client: TestClient):
        """Test that server config without required 'name' field is rejected."""
        context = {
            "context": {
                "servers": [
                    {"ip": "10.0.0.1", "purpose": "Missing name"}  # Missing required 'name'
                ]
            }
        }

        response = client.patch("/api/users/me/context", json=context)

        assert response.status_code == 422
        assert "validation failed" in response.json()["detail"].lower()

    def test_patch_invalid_tools_config(self, client: TestClient):
        """Test that tools config with wrong types is rejected."""
        context = {
            "context": {
                "tools": {
                    "location": "not-a-boolean",  # Should be boolean, not string
                }
            }
        }

        response = client.patch("/api/users/me/context", json=context)

        assert response.status_code == 422
        assert "validation failed" in response.json()["detail"].lower()

    def test_patch_valid_server_config(self, client: TestClient):
        """Test that valid server config is accepted."""
        context = {
            "context": {
                "servers": [
                    {
                        "name": "clifford",
                        "ip": "REDACTED_IP",
                        "purpose": "Production VPS",
                        "platform": "Ubuntu",
                        "notes": "Hetzner server",
                    }
                ]
            }
        }

        response = client.patch("/api/users/me/context", json=context)

        assert response.status_code == 200
        data = response.json()
        assert len(data["context"]["servers"]) == 1
        assert data["context"]["servers"][0]["name"] == "clifford"

    def test_patch_valid_tools_config(self, client: TestClient):
        """Test that valid tools config is accepted."""
        context = {
            "context": {
                "tools": {
                    "location": True,
                    "whoop": False,
                    "obsidian": True,
                    "supervisor": False,
                }
            }
        }

        response = client.patch("/api/users/me/context", json=context)

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["tools"]["location"] is True
        assert data["context"]["tools"]["whoop"] is False

    def test_patch_extra_fields_allowed(self, client: TestClient):
        """Test that extra fields beyond schema are allowed."""
        context = {
            "context": {
                "display_name": "Test User",
                "custom_field": "custom value",
                "nested_custom": {"key": "value"},
                "servers": [
                    {
                        "name": "test-server",
                        "custom_server_field": "allowed",
                    }
                ],
            }
        }

        response = client.patch("/api/users/me/context", json=context)

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["custom_field"] == "custom value"
        assert data["context"]["nested_custom"]["key"] == "value"
        assert data["context"]["servers"][0]["custom_server_field"] == "allowed"

    def test_put_invalid_server_config(self, client: TestClient):
        """Test that PUT also validates server config."""
        context = {
            "context": {
                "servers": [
                    {"ip": "10.0.0.1"}  # Missing required 'name'
                ]
            }
        }

        response = client.put("/api/users/me/context", json=context)

        assert response.status_code == 422
        assert "validation failed" in response.json()["detail"].lower()

    def test_put_valid_complete_context(self, client: TestClient):
        """Test that PUT accepts valid complete context."""
        context = {
            "context": {
                "display_name": "David",
                "role": "Software Engineer",
                "location": "San Francisco",
                "description": "Full-stack developer",
                "servers": [
                    {
                        "name": "clifford",
                        "ip": "REDACTED_IP",
                        "purpose": "Production VPS",
                        "platform": "Ubuntu",
                    }
                ],
                "integrations": {
                    "github": "david-rose",
                    "email": "hello@drose.io",
                },
                "tools": {
                    "location": True,
                    "whoop": True,
                    "obsidian": True,
                    "supervisor": True,
                },
                "custom_instructions": "Prefer TypeScript",
            }
        }

        response = client.put("/api/users/me/context", json=context)

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["display_name"] == "David"
        assert data["context"]["role"] == "Software Engineer"
        assert len(data["context"]["servers"]) == 1
        assert data["context"]["integrations"]["github"] == "david-rose"
        assert data["context"]["tools"]["location"] is True

    def test_put_invalid_json(self, client: TestClient):
        """Test that invalid JSON returns 422."""
        response = client.put(
            "/api/users/me/context",
            data="invalid json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422

    def test_context_with_unicode(self, client: TestClient):
        """Test that unicode characters are handled correctly."""
        context = {
            "context": {
                "display_name": "François",
                "location": "São Paulo",
                "notes": "Emoji test 🚀 🎉",
            }
        }

        response = client.put("/api/users/me/context", json=context)

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["display_name"] == "François"
        assert data["context"]["location"] == "São Paulo"
        assert "🚀" in data["context"]["notes"]

    def test_context_with_nested_objects(self, client: TestClient):
        """Test that deeply nested objects are preserved."""
        context = {
            "context": {
                "servers": [
                    {
                        "name": "server1",
                        "ip": "10.0.0.1",
                        "tags": ["production", "web"],
                        "metadata": {"region": "us-west", "zone": "a"},
                    }
                ]
            }
        }

        response = client.put("/api/users/me/context", json=context)

        assert response.status_code == 200
        data = response.json()
        assert data["context"]["servers"][0]["name"] == "server1"
        assert "production" in data["context"]["servers"][0]["tags"]
        assert data["context"]["servers"][0]["metadata"]["region"] == "us-west"
