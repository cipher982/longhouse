"""Unit tests for connector tester functions.

Tests the credential validation logic for each connector type,
including proper error handling and metadata extraction.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from zerg.connectors.testers import (
    _test_obsidian,
    _test_traccar,
    _test_whoop,
    test_connector,
)


class TestTestConnectorDispatch:
    """Tests for the test_connector dispatch function."""

    def test_unknown_connector_type(self):
        """Test that unknown connector types return error."""
        result = test_connector("nonexistent", {})
        assert result["success"] is False
        assert "Unknown connector type" in result["message"]

    def test_dispatch_to_correct_tester(self):
        """Test that test_connector dispatches to the correct tester."""
        # Test with obsidian which doesn't need network calls
        result = test_connector("obsidian", {"vault_path": "~/notes", "runner_name": "laptop"})
        assert result["success"] is True
        assert "runner_name" in result["metadata"]


class TestTraccarTester:
    """Tests for the Traccar connector tester."""

    def test_missing_url(self):
        """Test error when URL is missing."""
        result = _test_traccar({"username": "admin", "password": "pass"})
        assert result["success"] is False
        assert "Missing url" in result["message"]

    def test_missing_username(self):
        """Test error when username is missing."""
        result = _test_traccar({"url": "http://traccar.example.com", "password": "pass"})
        assert result["success"] is False
        assert "Missing username" in result["message"]

    def test_missing_password(self):
        """Test error when password is missing."""
        result = _test_traccar({"url": "http://traccar.example.com", "username": "admin"})
        assert result["success"] is False
        assert "Missing password" in result["message"]

    @patch("zerg.connectors.testers.httpx.post")
    def test_successful_auth(self, mock_post):
        """Test successful Traccar authentication."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "Admin User", "email": "admin@example.com"}
        mock_post.return_value = mock_response

        result = _test_traccar({
            "url": "http://traccar.example.com",
            "username": "admin",
            "password": "pass",
        })

        assert result["success"] is True
        assert "Admin User" in result["message"]
        assert result["metadata"]["user"] == "Admin User"
        assert result["metadata"]["server"] == "http://traccar.example.com"

    @patch("zerg.connectors.testers.httpx.post")
    def test_invalid_credentials(self, mock_post):
        """Test error on invalid credentials."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_post.return_value = mock_response

        result = _test_traccar({
            "url": "http://traccar.example.com",
            "username": "admin",
            "password": "wrong",
        })

        assert result["success"] is False
        assert "Invalid username or password" in result["message"]

    @patch("zerg.connectors.testers.httpx.get")
    @patch("zerg.connectors.testers.httpx.post")
    def test_with_valid_device_id(self, mock_post, mock_get):
        """Test successful auth with valid device_id verification."""
        # Mock session response
        mock_session = MagicMock()
        mock_session.status_code = 200
        mock_session.json.return_value = {"name": "Admin"}
        mock_session.cookies = {"JSESSIONID": "abc123"}
        mock_post.return_value = mock_session

        # Mock devices response
        mock_devices = MagicMock()
        mock_devices.status_code = 200
        mock_devices.json.return_value = [
            {"id": 1, "name": "My Phone"},
            {"id": 2, "name": "Car Tracker"},
        ]
        mock_get.return_value = mock_devices

        result = _test_traccar({
            "url": "http://traccar.example.com",
            "username": "admin",
            "password": "pass",
            "device_id": "1",
        })

        assert result["success"] is True
        assert result["metadata"]["device"] == "My Phone"

    @patch("zerg.connectors.testers.httpx.get")
    @patch("zerg.connectors.testers.httpx.post")
    def test_with_invalid_device_id(self, mock_post, mock_get):
        """Test error when device_id doesn't exist."""
        mock_session = MagicMock()
        mock_session.status_code = 200
        mock_session.json.return_value = {"name": "Admin"}
        mock_session.cookies = {"JSESSIONID": "abc123"}
        mock_post.return_value = mock_session

        mock_devices = MagicMock()
        mock_devices.status_code = 200
        mock_devices.json.return_value = [{"id": 1, "name": "My Phone"}]
        mock_get.return_value = mock_devices

        result = _test_traccar({
            "url": "http://traccar.example.com",
            "username": "admin",
            "password": "pass",
            "device_id": "999",
        })

        assert result["success"] is False
        assert "Device ID 999 not found" in result["message"]

    @patch("zerg.connectors.testers.httpx.post")
    def test_url_normalization(self, mock_post):
        """Test that trailing slashes are stripped from URL."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"name": "Admin"}
        mock_post.return_value = mock_response

        _test_traccar({
            "url": "http://traccar.example.com/",
            "username": "admin",
            "password": "pass",
        })

        # Verify the URL was normalized
        call_args = mock_post.call_args
        assert call_args[0][0] == "http://traccar.example.com/api/session"


class TestWhoopTester:
    """Tests for the WHOOP connector tester."""

    def test_missing_access_token(self):
        """Test error when access_token is missing."""
        result = _test_whoop({})
        assert result["success"] is False
        assert "Missing access_token" in result["message"]

    @patch("zerg.connectors.testers.httpx.get")
    def test_successful_auth(self, mock_get):
        """Test successful WHOOP authentication."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "user_id": 12345,
            "first_name": "John",
            "last_name": "Doe",
        }
        mock_get.return_value = mock_response

        result = _test_whoop({"access_token": "valid_token"})

        assert result["success"] is True
        assert "John Doe" in result["message"]
        assert result["metadata"]["user_id"] == 12345
        assert result["metadata"]["name"] == "John Doe"

    @patch("zerg.connectors.testers.httpx.get")
    def test_expired_token(self, mock_get):
        """Test error on expired/invalid token."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        result = _test_whoop({"access_token": "expired_token"})

        assert result["success"] is False
        assert "Invalid or expired" in result["message"]

    @patch("zerg.connectors.testers.httpx.get")
    def test_insufficient_scopes(self, mock_get):
        """Test error when token lacks required scopes."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        result = _test_whoop({"access_token": "limited_token"})

        assert result["success"] is False
        assert "lacks required scopes" in result["message"]

    @patch("zerg.connectors.testers.httpx.get")
    def test_user_without_name(self, mock_get):
        """Test handling user profile without name fields."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"user_id": 12345}
        mock_get.return_value = mock_response

        result = _test_whoop({"access_token": "valid_token"})

        assert result["success"] is True
        assert result["metadata"]["name"] == "WHOOP User"


class TestObsidianTester:
    """Tests for the Obsidian connector tester."""

    def test_missing_vault_path(self):
        """Test error when vault_path is missing."""
        result = _test_obsidian({"runner_name": "laptop"})
        assert result["success"] is False
        assert "Missing vault_path" in result["message"]

    def test_missing_runner_name(self):
        """Test error when runner_name is missing."""
        result = _test_obsidian({"vault_path": "~/notes"})
        assert result["success"] is False
        assert "Missing runner_name" in result["message"]

    def test_successful_config(self):
        """Test successful Obsidian configuration."""
        result = _test_obsidian({
            "vault_path": "~/obsidian_vault",
            "runner_name": "macbook",
        })

        assert result["success"] is True
        assert "macbook" in result["message"]
        assert result["metadata"]["vault_path"] == "~/obsidian_vault"
        assert result["metadata"]["runner_name"] == "macbook"


class TestTesterErrorHandling:
    """Tests for error handling in the test_connector function."""

    @patch("zerg.connectors.testers.httpx.post")
    def test_timeout_handling(self, mock_post):
        """Test that timeouts are handled gracefully."""
        mock_post.side_effect = httpx.TimeoutException("Connection timed out")

        result = test_connector("traccar", {
            "url": "http://traccar.example.com",
            "username": "admin",
            "password": "pass",
        })

        assert result["success"] is False
        assert "timed out" in result["message"]

    @patch("zerg.connectors.testers.httpx.post")
    def test_connection_error_handling(self, mock_post):
        """Test that connection errors are handled gracefully."""
        mock_post.side_effect = httpx.ConnectError("Failed to connect")

        result = test_connector("traccar", {
            "url": "http://traccar.example.com",
            "username": "admin",
            "password": "pass",
        })

        assert result["success"] is False
        assert "Failed to connect" in result["message"]
