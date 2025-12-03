"""Tests for connector status builder.

This module tests the build_connector_status and build_agent_context functions
that create structured status information for agent prompt injection.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from zerg.connectors.registry import ConnectorType
from zerg.connectors.status_builder import (
    build_agent_context,
    build_connector_status,
    get_capabilities_for_connector,
    get_tools_for_connector,
)
from zerg.models.models import User


def test_get_tools_for_connector_github():
    """Test that get_tools_for_connector returns correct tool list for GitHub."""
    tools = get_tools_for_connector(ConnectorType.GITHUB)

    assert isinstance(tools, list)
    assert len(tools) > 0
    assert "github_list_repositories" in tools
    assert "github_create_issue" in tools
    assert "github_list_issues" in tools
    assert "github_get_issue" in tools
    assert "github_add_comment" in tools
    assert "github_list_pull_requests" in tools
    assert "github_get_pull_request" in tools


def test_get_tools_for_connector_slack():
    """Test that get_tools_for_connector returns correct tool list for Slack."""
    tools = get_tools_for_connector(ConnectorType.SLACK)

    assert isinstance(tools, list)
    assert len(tools) > 0
    assert "send_slack_webhook" in tools


def test_get_tools_for_connector_jira():
    """Test that get_tools_for_connector returns correct tool list for Jira."""
    tools = get_tools_for_connector(ConnectorType.JIRA)

    assert isinstance(tools, list)
    assert len(tools) > 0
    assert "jira_create_issue" in tools
    assert "jira_list_issues" in tools
    assert "jira_get_issue" in tools
    assert "jira_add_comment" in tools
    assert "jira_transition_issue" in tools
    assert "jira_update_issue" in tools


def test_get_tools_for_connector_unknown():
    """Test that get_tools_for_connector returns empty list for unknown connectors."""
    # This shouldn't happen with the enum, but test defensive programming
    # Create a mock ConnectorType that's not in the mapping
    tools = get_tools_for_connector("nonexistent_connector")  # type: ignore

    assert isinstance(tools, list)
    assert len(tools) == 0


def test_get_capabilities_for_connector_github():
    """Test that get_capabilities_for_connector returns descriptive capabilities for GitHub."""
    capabilities = get_capabilities_for_connector(ConnectorType.GITHUB)

    assert isinstance(capabilities, list)
    assert len(capabilities) > 0
    # Check that capabilities are descriptive human-readable strings
    assert any("issue" in cap.lower() for cap in capabilities)
    assert any("repositories" in cap.lower() or "repository" in cap.lower() for cap in capabilities)


def test_get_capabilities_for_connector_slack():
    """Test that get_capabilities_for_connector returns descriptive capabilities for Slack."""
    capabilities = get_capabilities_for_connector(ConnectorType.SLACK)

    assert isinstance(capabilities, list)
    assert len(capabilities) > 0
    # Check that capabilities mention messaging
    assert any("message" in cap.lower() or "channel" in cap.lower() for cap in capabilities)


def test_get_capabilities_for_connector_all_connectors():
    """Test that all connector types have capability descriptions."""
    for connector_type in ConnectorType:
        capabilities = get_capabilities_for_connector(connector_type)
        assert isinstance(capabilities, list)
        assert len(capabilities) > 0, f"No capabilities defined for {connector_type.value}"


@pytest.mark.parametrize(
    "connector_type,expected_in_tools",
    [
        (ConnectorType.GITHUB, "github_create_issue"),
        (ConnectorType.SLACK, "send_slack_webhook"),
        (ConnectorType.DISCORD, "send_discord_webhook"),
        (ConnectorType.EMAIL, "send_email"),
        (ConnectorType.SMS, "send_sms"),
        (ConnectorType.JIRA, "jira_create_issue"),
        (ConnectorType.LINEAR, "linear_create_issue"),
        (ConnectorType.NOTION, "notion_query_database"),
        (ConnectorType.IMESSAGE, "send_imessage"),
    ],
)
def test_get_tools_for_connector_all_types(connector_type, expected_in_tools):
    """Test that each connector type has at least one expected tool."""
    tools = get_tools_for_connector(connector_type)
    assert expected_in_tools in tools


def test_build_connector_status_no_connectors(db_session: Session, test_user: User):
    """Test build_connector_status returns all 'not_configured' when nothing is configured."""
    # Mock the CredentialResolver to return empty set
    with patch("zerg.connectors.status_builder.CredentialResolver") as mock_resolver_class:
        mock_resolver = MagicMock()
        mock_resolver.get_all_configured.return_value = []
        mock_resolver_class.return_value = mock_resolver

        status = build_connector_status(
            db=db_session,
            owner_id=test_user.id,
            agent_id=None,
        )

        # Should have status for all connector types
        assert len(status) == len(ConnectorType)

        # All should be not_configured
        for connector_type in ConnectorType:
            connector_status = status[connector_type.value]
            assert connector_status["status"] == "not_configured"
            assert "setup_url" in connector_status
            assert connector_status["setup_url"] == "/settings/integrations"
            assert "would_enable" in connector_status
            assert len(connector_status["would_enable"]) > 0
            # Not_configured shouldn't have tools
            assert "tools" not in connector_status


def test_build_connector_status_with_configured(db_session: Session, test_user: User):
    """Test build_connector_status returns 'connected' for configured connectors."""
    # Mock the CredentialResolver to return GitHub and Slack as configured
    with patch("zerg.connectors.status_builder.CredentialResolver") as mock_resolver_class:
        mock_resolver = MagicMock()
        mock_resolver.get_all_configured.return_value = ["github", "slack"]
        mock_resolver_class.return_value = mock_resolver

        status = build_connector_status(
            db=db_session,
            owner_id=test_user.id,
            agent_id=None,
        )

        # GitHub should be connected
        assert status["github"]["status"] == "connected"
        assert "tools" in status["github"]
        assert len(status["github"]["tools"]) > 0
        assert "github_create_issue" in status["github"]["tools"]
        assert "would_enable" in status["github"]

        # Slack should be connected
        assert status["slack"]["status"] == "connected"
        assert "tools" in status["slack"]
        assert "send_slack_webhook" in status["slack"]["tools"]
        assert "would_enable" in status["slack"]

        # Discord should not be configured
        assert status["discord"]["status"] == "not_configured"
        assert "setup_url" in status["discord"]
        assert "tools" not in status["discord"]


def test_build_connector_status_with_agent_id(db_session: Session, test_user: User):
    """Test build_connector_status passes agent_id to resolver correctly."""
    agent_id = 42

    with patch("zerg.connectors.status_builder.CredentialResolver") as mock_resolver_class:
        mock_resolver = MagicMock()
        mock_resolver.get_all_configured.return_value = ["github"]
        mock_resolver_class.return_value = mock_resolver

        status = build_connector_status(
            db=db_session,
            owner_id=test_user.id,
            agent_id=agent_id,
        )

        # Verify resolver was instantiated with correct agent_id
        mock_resolver_class.assert_called_once()
        call_kwargs = mock_resolver_class.call_args[1]
        assert call_kwargs["agent_id"] == agent_id
        assert call_kwargs["owner_id"] == test_user.id
        assert call_kwargs["db"] == db_session

        # Status should reflect configured connector
        assert status["github"]["status"] == "connected"


def test_build_agent_context_format(db_session: Session, test_user: User):
    """Test build_agent_context returns properly formatted XML with JSON."""
    # Mock the CredentialResolver
    with patch("zerg.connectors.status_builder.CredentialResolver") as mock_resolver_class:
        mock_resolver = MagicMock()
        mock_resolver.get_all_configured.return_value = ["github", "slack"]
        mock_resolver_class.return_value = mock_resolver

        context = build_agent_context(
            db=db_session,
            owner_id=test_user.id,
            agent_id=None,
        )

        # Should be a string
        assert isinstance(context, str)

        # Should contain current_time XML tag
        assert "<current_time>" in context
        assert "</current_time>" in context

        # Should contain connector_status XML tag with captured_at attribute
        assert "<connector_status captured_at=" in context
        assert "</connector_status>" in context

        # Should be valid JSON inside connector_status block
        # Extract the JSON portion
        start_idx = context.find('">') + 2
        end_idx = context.find("</connector_status>")
        json_str = context[start_idx:end_idx].strip()

        # Parse JSON to verify it's valid
        connector_data = json.loads(json_str)
        assert isinstance(connector_data, dict)
        assert "github" in connector_data
        assert "slack" in connector_data
        assert connector_data["github"]["status"] == "connected"
        assert connector_data["slack"]["status"] == "connected"


def test_build_agent_context_timestamp_format(db_session: Session, test_user: User):
    """Test build_agent_context uses correct ISO 8601 timestamp format with Z suffix."""
    with patch("zerg.connectors.status_builder.CredentialResolver") as mock_resolver_class:
        mock_resolver = MagicMock()
        mock_resolver.get_all_configured.return_value = []
        mock_resolver_class.return_value = mock_resolver

        # Mock datetime to control timestamp
        mock_time = datetime(2025, 1, 17, 15, 30, 45, tzinfo=timezone.utc)
        with patch("zerg.connectors.status_builder.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_time
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            context = build_agent_context(
                db=db_session,
                owner_id=test_user.id,
                agent_id=None,
            )

            # Should contain the exact timestamp we mocked
            expected_timestamp = "2025-01-17T15:30:45Z"
            assert expected_timestamp in context

            # Should appear in both current_time and captured_at
            assert f"<current_time>{expected_timestamp}</current_time>" in context
            assert f'captured_at="{expected_timestamp}"' in context


def test_build_agent_context_includes_all_connector_types(db_session: Session, test_user: User):
    """Test build_agent_context includes status for all connector types."""
    with patch("zerg.connectors.status_builder.CredentialResolver") as mock_resolver_class:
        mock_resolver = MagicMock()
        mock_resolver.get_all_configured.return_value = []
        mock_resolver_class.return_value = mock_resolver

        context = build_agent_context(
            db=db_session,
            owner_id=test_user.id,
            agent_id=None,
        )

        # Extract and parse JSON
        start_idx = context.find('">') + 2
        end_idx = context.find("</connector_status>")
        json_str = context[start_idx:end_idx].strip()
        connector_data = json.loads(json_str)

        # Should have all connector types
        assert len(connector_data) == len(ConnectorType)
        for connector_type in ConnectorType:
            assert connector_type.value in connector_data


def test_build_connector_status_mixed_connectors(db_session: Session, test_user: User):
    """Test build_connector_status with mix of configured and not configured."""
    with patch("zerg.connectors.status_builder.CredentialResolver") as mock_resolver_class:
        mock_resolver = MagicMock()
        # Configure some but not all
        mock_resolver.get_all_configured.return_value = ["github", "jira", "notion"]
        mock_resolver_class.return_value = mock_resolver

        status = build_connector_status(
            db=db_session,
            owner_id=test_user.id,
            agent_id=None,
        )

        # Configured ones should be connected
        configured = ["github", "jira", "notion"]
        for connector in configured:
            assert status[connector]["status"] == "connected"
            assert "tools" in status[connector]
            assert len(status[connector]["tools"]) > 0

        # Not configured ones should have not_configured status
        not_configured = [ct.value for ct in ConnectorType if ct.value not in configured]
        for connector in not_configured:
            assert status[connector]["status"] == "not_configured"
            assert "setup_url" in status[connector]
            assert "tools" not in status[connector]
