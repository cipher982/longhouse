"""Tests for Jarvis BFF (Backend-for-Frontend) endpoints.

Tests the proxy endpoints and bootstrap endpoint that make zerg-backend
the single API surface for Jarvis.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import httpx


class TestBootstrapEndpoint:
    """Tests for GET /api/jarvis/bootstrap."""

    def test_bootstrap_requires_auth_when_auth_enabled(self, client):
        """Bootstrap returns 401 when AUTH_DISABLED=0 and no token is provided."""
        import zerg.dependencies.auth as auth

        prev = auth.AUTH_DISABLED
        auth.AUTH_DISABLED = False
        try:
            response = client.get("/api/jarvis/bootstrap")
            assert response.status_code == 401
        finally:
            auth.AUTH_DISABLED = prev

    def test_bootstrap_returns_prompt_and_tools(self, client, test_user, db_session):
        """Bootstrap endpoint returns prompt, tools, and user context."""
        # Set up user context
        test_user.context = {
            "display_name": "David",
            "role": "software engineer",
            "location": "Nashville, TN",
            "servers": [
                {"name": "clifford", "ip": "1.2.3.4", "purpose": "Production VPS"},
                {"name": "cube", "ip": "5.6.7.8", "purpose": "GPU server"},
            ],
        }
        db_session.add(test_user)
        db_session.commit()

        response = client.get("/api/jarvis/bootstrap")

        assert response.status_code == 200
        data = response.json()

        # Check structure
        assert "prompt" in data
        assert "enabled_tools" in data
        assert "user_context" in data

        # Check prompt contains user context
        assert "David" in data["prompt"]

        # Check tools list
        tool_names = [t["name"] for t in data["enabled_tools"]]
        assert "get_current_location" in tool_names
        assert "route_to_supervisor" in tool_names

        # Check user context is redacted (no IPs)
        assert data["user_context"]["display_name"] == "David"
        assert len(data["user_context"]["servers"]) == 2

    def test_bootstrap_with_empty_context(self, client, test_user, db_session):
        """Bootstrap works with empty user context."""
        test_user.context = {}
        db_session.add(test_user)
        db_session.commit()

        response = client.get("/api/jarvis/bootstrap")

        assert response.status_code == 200
        data = response.json()

        # Should still have prompt and tools
        assert "prompt" in data
        assert "enabled_tools" in data
        assert len(data["enabled_tools"]) > 0

    def test_bootstrap_filters_disabled_tools(self, client, test_user, db_session):
        """Bootstrap respects user tool configuration."""
        test_user.context = {
            "display_name": "David",
            "tools": {
                "location": True,
                "whoop": False,  # Disabled
                "obsidian": False,  # Disabled
                "supervisor": True,
            },
        }
        db_session.add(test_user)
        db_session.commit()

        response = client.get("/api/jarvis/bootstrap")

        assert response.status_code == 200
        data = response.json()

        # Check only enabled tools are returned
        tool_names = [t["name"] for t in data["enabled_tools"]]
        assert "get_current_location" in tool_names
        assert "route_to_supervisor" in tool_names
        assert "get_whoop_data" not in tool_names
        assert "search_notes" not in tool_names
        assert len(data["enabled_tools"]) == 2

    def test_bootstrap_defaults_to_all_tools_enabled(self, client, test_user, db_session):
        """Bootstrap enables all tools by default if not configured."""
        test_user.context = {
            "display_name": "David",
            # No tools key - should default all enabled
        }
        db_session.add(test_user)
        db_session.commit()

        response = client.get("/api/jarvis/bootstrap")

        assert response.status_code == 200
        data = response.json()

        # Should have all 4 tools
        tool_names = [t["name"] for t in data["enabled_tools"]]
        assert "get_current_location" in tool_names
        assert "get_whoop_data" in tool_names
        assert "search_notes" in tool_names
        assert "route_to_supervisor" in tool_names
        assert len(data["enabled_tools"]) == 4


class TestSessionEndpoint:
    """Tests for /api/jarvis/session endpoint (direct OpenAI integration)."""

    def test_session_returns_token(self, client):
        """Session endpoint returns OpenAI Realtime token."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"client_secret": {"value": "test-token"}}
        mock_response.raise_for_status = MagicMock()

        with patch("zerg.services.openai_realtime.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            response = client.get("/api/jarvis/session")

        assert response.status_code == 200
        data = response.json()
        assert "client_secret" in data

    def test_session_handles_timeout(self, client):
        """Session endpoint returns 504 on timeout."""
        with patch("zerg.services.openai_realtime.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            response = client.get("/api/jarvis/session")

        assert response.status_code == 504
        assert "timeout" in response.json()["detail"].lower()

    def test_session_handles_openai_error(self, client):
        """Session endpoint returns appropriate error on OpenAI API error."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("zerg.services.openai_realtime.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            error = httpx.HTTPStatusError("Unauthorized", request=MagicMock(), response=mock_response)
            mock_client.post = AsyncMock(side_effect=error)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            response = client.get("/api/jarvis/session")

        assert response.status_code == 401


# NOTE: /api/jarvis/tool endpoint was removed - MCP tools were disabled in production
# and tool execution now goes through the supervisor/worker system.


class TestBootstrapPromptIntegration:
    """Integration tests for bootstrap prompt generation."""

    def test_bootstrap_prompt_reflects_user_context(self, client, test_user, db_session):
        """Bootstrap prompt includes user's configured servers."""
        test_user.context = {
            "display_name": "David",
            "servers": [
                {"name": "clifford", "purpose": "Production VPS"},
                {"name": "cube", "purpose": "GPU server"},
            ],
        }
        db_session.add(test_user)
        db_session.commit()

        response = client.get("/api/jarvis/bootstrap")

        data = response.json()
        prompt = data["prompt"]

        # Prompt should mention servers from user context
        assert "clifford" in prompt or "cube" in prompt

    def test_bootstrap_tools_match_prompt_claims(self, client, test_user, db_session):
        """Tools list matches what the prompt claims is available."""
        test_user.context = {"display_name": "David"}
        db_session.add(test_user)
        db_session.commit()

        response = client.get("/api/jarvis/bootstrap")

        data = response.json()
        tool_names = {t["name"] for t in data["enabled_tools"]}

        # Should have the expected tools
        assert "get_current_location" in tool_names
        assert "route_to_supervisor" in tool_names
