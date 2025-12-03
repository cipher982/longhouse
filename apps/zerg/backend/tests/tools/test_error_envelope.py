"""Tests for error envelope module."""

import pytest

from zerg.tools.error_envelope import (
    ErrorType,
    tool_error,
    tool_success,
    connector_not_configured_error,
    invalid_credentials_error,
)


class TestErrorType:
    """Test the ErrorType enum."""

    def test_error_type_values(self):
        """Test that all expected error types are defined."""
        assert ErrorType.CONNECTOR_NOT_CONFIGURED == "connector_not_configured"
        assert ErrorType.INVALID_CREDENTIALS == "invalid_credentials"
        assert ErrorType.RATE_LIMITED == "rate_limited"
        assert ErrorType.PERMISSION_DENIED == "permission_denied"
        assert ErrorType.VALIDATION_ERROR == "validation_error"
        assert ErrorType.EXECUTION_ERROR == "execution_error"


class TestToolError:
    """Test the tool_error function."""

    def test_tool_error_basic(self):
        """Test basic error response creation."""
        result = tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Something went wrong",
        )

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
        assert result["user_message"] == "Something went wrong"
        assert "connector" not in result
        assert "setup_url" not in result

    def test_tool_error_with_connector(self):
        """Test error response with connector info."""
        result = tool_error(
            error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
            user_message="GitHub not connected",
            connector="github",
        )

        assert result["ok"] is False
        assert result["error_type"] == "connector_not_configured"
        assert result["user_message"] == "GitHub not connected"
        assert result["connector"] == "github"

    def test_tool_error_with_setup_url(self):
        """Test error response with setup URL."""
        result = tool_error(
            error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
            user_message="Slack not connected",
            connector="slack",
            setup_url="/settings/integrations",
        )

        assert result["ok"] is False
        assert result["error_type"] == "connector_not_configured"
        assert result["user_message"] == "Slack not connected"
        assert result["connector"] == "slack"
        assert result["setup_url"] == "/settings/integrations"

    def test_tool_error_all_error_types(self):
        """Test that all error types can be used."""
        for error_type in ErrorType:
            result = tool_error(
                error_type=error_type,
                user_message=f"Test {error_type.value}",
            )
            assert result["ok"] is False
            assert result["error_type"] == error_type.value


class TestToolSuccess:
    """Test the tool_success function."""

    def test_tool_success_with_dict(self):
        """Test success response with dict data."""
        data = {"result": "success", "count": 5}
        result = tool_success(data)

        assert result["ok"] is True
        assert result["data"] == data

    def test_tool_success_with_string(self):
        """Test success response with string data."""
        result = tool_success("Operation completed")

        assert result["ok"] is True
        assert result["data"] == "Operation completed"

    def test_tool_success_with_list(self):
        """Test success response with list data."""
        data = ["item1", "item2", "item3"]
        result = tool_success(data)

        assert result["ok"] is True
        assert result["data"] == data

    def test_tool_success_with_none(self):
        """Test success response with None data."""
        result = tool_success(None)

        assert result["ok"] is True
        assert result["data"] is None

    def test_tool_success_with_complex_data(self):
        """Test success response with complex nested data."""
        data = {
            "repositories": [
                {"name": "repo1", "stars": 10},
                {"name": "repo2", "stars": 20},
            ],
            "count": 2,
            "has_more": False,
        }
        result = tool_success(data)

        assert result["ok"] is True
        assert result["data"] == data
        assert len(result["data"]["repositories"]) == 2


class TestConnectorNotConfiguredError:
    """Test the connector_not_configured_error helper."""

    def test_with_default_display_name(self):
        """Test with default display name (title case)."""
        result = connector_not_configured_error("github")

        assert result["ok"] is False
        assert result["error_type"] == "connector_not_configured"
        assert "Github" in result["user_message"]
        assert "Settings → Integrations → Github" in result["user_message"]
        assert result["connector"] == "github"
        assert result["setup_url"] == "/settings/integrations"

    def test_with_custom_display_name(self):
        """Test with custom display name."""
        result = connector_not_configured_error("slack", "Slack Workspace")

        assert result["ok"] is False
        assert result["error_type"] == "connector_not_configured"
        assert "Slack Workspace" in result["user_message"]
        assert "Settings → Integrations → Slack Workspace" in result["user_message"]
        assert result["connector"] == "slack"
        assert result["setup_url"] == "/settings/integrations"

    def test_multiple_connectors(self):
        """Test creating errors for different connectors."""
        connectors = [
            ("github", "GitHub"),
            ("slack", "Slack"),
            ("discord", "Discord"),
            ("email", "Email"),
        ]

        for connector, display_name in connectors:
            result = connector_not_configured_error(connector, display_name)
            assert result["ok"] is False
            assert result["connector"] == connector
            assert display_name in result["user_message"]


class TestInvalidCredentialsError:
    """Test the invalid_credentials_error helper."""

    def test_with_default_display_name(self):
        """Test with default display name (title case)."""
        result = invalid_credentials_error("github")

        assert result["ok"] is False
        assert result["error_type"] == "invalid_credentials"
        assert "Github" in result["user_message"]
        assert "expired" in result["user_message"].lower()
        assert "reconnect" in result["user_message"].lower()
        assert result["connector"] == "github"
        assert result["setup_url"] == "/settings/integrations"

    def test_with_custom_display_name(self):
        """Test with custom display name."""
        result = invalid_credentials_error("slack", "Slack Workspace")

        assert result["ok"] is False
        assert result["error_type"] == "invalid_credentials"
        assert "Slack Workspace" in result["user_message"]
        assert "expired" in result["user_message"].lower()
        assert result["connector"] == "slack"
        assert result["setup_url"] == "/settings/integrations"

    def test_multiple_connectors(self):
        """Test creating errors for different connectors."""
        connectors = [
            ("github", "GitHub"),
            ("notion", "Notion"),
            ("jira", "Jira"),
        ]

        for connector, display_name in connectors:
            result = invalid_credentials_error(connector, display_name)
            assert result["ok"] is False
            assert result["error_type"] == "invalid_credentials"
            assert result["connector"] == connector
            assert display_name in result["user_message"]


class TestResponseStructure:
    """Test the overall response structure."""

    def test_error_response_keys(self):
        """Test that error responses have correct keys."""
        result = tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Invalid input",
            connector="test",
            setup_url="/setup",
        )

        # Required keys
        assert "ok" in result
        assert "error_type" in result
        assert "user_message" in result

        # Optional keys (present in this case)
        assert "connector" in result
        assert "setup_url" in result

    def test_success_response_keys(self):
        """Test that success responses have correct keys."""
        result = tool_success({"test": "data"})

        # Required keys
        assert "ok" in result
        assert "data" in result

        # Should not have error keys
        assert "error_type" not in result
        assert "user_message" not in result

    def test_error_and_success_are_distinguishable(self):
        """Test that error and success responses can be easily distinguished."""
        error = tool_error(ErrorType.EXECUTION_ERROR, "Failed")
        success = tool_success({"result": "ok"})

        # Both have 'ok' key
        assert "ok" in error
        assert "ok" in success

        # But different values
        assert error["ok"] is False
        assert success["ok"] is True

        # Error has error_type, success has data
        assert "error_type" in error
        assert "data" in success
        assert "data" not in error
        assert "error_type" not in success
