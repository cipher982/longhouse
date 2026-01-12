"""Tests for connector meta-tools."""

from contextlib import contextmanager
from unittest.mock import MagicMock
from unittest.mock import patch

from zerg.tools.builtin.connector_tools import refresh_connector_status


@contextmanager
def _fake_db_session():
    yield MagicMock()


class TestRefreshConnectorStatus:
    """Tests for refresh_connector_status tool."""

    def test_returns_error_when_no_resolver_context(self):
        """Should return error when called outside agent context."""
        with patch("zerg.tools.builtin.connector_tools.get_credential_resolver", return_value=None):
            result = refresh_connector_status()

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
        assert "no credential context" in result["user_message"]

    def test_returns_connector_status_when_resolver_available(self):
        """Should return connector status when resolver context is available."""
        mock_resolver = MagicMock()
        mock_resolver.owner_id = 1
        mock_resolver.agent_id = 42

        mock_status = {
            "github": {"status": "connected", "tools": ["github_create_issue"]},
            "slack": {"status": "not_configured", "setup_url": "/settings/integrations/slack"},
        }

        with patch("zerg.tools.builtin.connector_tools.get_credential_resolver", return_value=mock_resolver):
            with patch("zerg.tools.builtin.connector_tools.db_session", _fake_db_session):
                with patch("zerg.tools.builtin.connector_tools.build_connector_status", return_value=mock_status):
                    result = refresh_connector_status()

        assert result["ok"] is True
        assert result["data"] == mock_status
        assert result["data"]["github"]["status"] == "connected"
        assert result["data"]["slack"]["status"] == "not_configured"

    def test_returns_error_on_build_exception(self):
        """Should return error if build_connector_status raises exception."""
        mock_resolver = MagicMock()
        mock_resolver.owner_id = 1
        mock_resolver.agent_id = 42

        with patch("zerg.tools.builtin.connector_tools.get_credential_resolver", return_value=mock_resolver):
            with patch("zerg.tools.builtin.connector_tools.db_session", _fake_db_session):
                with patch(
                    "zerg.tools.builtin.connector_tools.build_connector_status",
                    side_effect=Exception("DB connection failed"),
                ):
                    result = refresh_connector_status()

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
        assert "DB connection failed" in result["user_message"]

    def test_tool_is_registered(self):
        """Verify tool is in BUILTIN_TOOLS."""
        from zerg.tools.builtin import BUILTIN_TOOLS

        tool_names = [t.name for t in BUILTIN_TOOLS]
        assert "refresh_connector_status" in tool_names
