"""Build connector status for injection into agent prompts.

This module provides functions to query connector configuration and build
structured status information for agent context injection.

The status builder:
- Uses CredentialResolver to check which connectors are configured
- Enriches with metadata from the connector registry
- Returns structured status for all connectors (connected, not_configured, invalid_credentials)
- Builds XML-formatted context strings for agent prompts
"""

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from zerg.connectors.registry import CONNECTOR_REGISTRY, ConnectorType
from zerg.connectors.resolver import CredentialResolver

logger = logging.getLogger(__name__)

# Mapping of connector types to the tools that require them
CONNECTOR_TOOL_MAPPING = {
    ConnectorType.GITHUB: [
        "github_list_repositories",
        "github_create_issue",
        "github_list_issues",
        "github_get_issue",
        "github_add_comment",
        "github_list_pull_requests",
        "github_get_pull_request",
    ],
    ConnectorType.SLACK: [
        "send_slack_webhook",
    ],
    ConnectorType.DISCORD: [
        "send_discord_webhook",
    ],
    ConnectorType.EMAIL: [
        "send_email",
    ],
    ConnectorType.SMS: [
        "send_sms",
    ],
    ConnectorType.JIRA: [
        "jira_create_issue",
        "jira_list_issues",
        "jira_get_issue",
        "jira_add_comment",
        "jira_transition_issue",
        "jira_update_issue",
    ],
    ConnectorType.LINEAR: [
        "linear_create_issue",
        "linear_list_issues",
        "linear_get_issue",
        "linear_update_issue",
    ],
    ConnectorType.NOTION: [
        "notion_query_database",
        "notion_get_page",
        "notion_create_page",
        "notion_update_page",
    ],
    ConnectorType.IMESSAGE: [
        "send_imessage",
    ],
}

# Human-readable capability descriptions for each connector
CONNECTOR_CAPABILITIES = {
    ConnectorType.GITHUB: [
        "Create issues and pull requests",
        "List and search repositories",
        "Add comments to issues and PRs",
        "Manage issue lifecycle",
    ],
    ConnectorType.SLACK: [
        "Send messages to channels",
        "Post formatted notifications",
        "Share updates with teams",
    ],
    ConnectorType.DISCORD: [
        "Send messages to Discord channels",
        "Post announcements",
        "Share notifications",
    ],
    ConnectorType.EMAIL: [
        "Send emails via Resend",
        "Deliver notifications and alerts",
        "Send formatted messages",
    ],
    ConnectorType.SMS: [
        "Send SMS messages via Twilio",
        "Deliver urgent alerts",
        "Send mobile notifications",
    ],
    ConnectorType.JIRA: [
        "Create and manage Jira issues",
        "Update issue status and fields",
        "Add comments to tickets",
        "Search and list issues",
    ],
    ConnectorType.LINEAR: [
        "Create and manage Linear issues",
        "Update issue properties",
        "Track project progress",
    ],
    ConnectorType.NOTION: [
        "Query and search databases",
        "Create and update pages",
        "Manage workspace content",
    ],
    ConnectorType.IMESSAGE: [
        "Send iMessages (requires macOS host)",
        "Deliver mobile notifications",
    ],
}


def get_tools_for_connector(connector_type: ConnectorType) -> list[str]:
    """Return list of tool names that require this connector.

    Args:
        connector_type: The connector type to look up

    Returns:
        List of tool names that require this connector
    """
    return CONNECTOR_TOOL_MAPPING.get(connector_type, [])


def get_capabilities_for_connector(connector_type: ConnectorType) -> list[str]:
    """Return human-readable capability descriptions for this connector.

    Args:
        connector_type: The connector type to look up

    Returns:
        List of human-readable capability descriptions
    """
    return CONNECTOR_CAPABILITIES.get(connector_type, [])


def build_connector_status(
    db: "Session",
    owner_id: int,
    agent_id: int | None = None,
) -> dict[str, Any]:
    """Build connector status dict for all connectors.

    Uses the credential resolver to determine which connectors are configured,
    then enriches with metadata from the connector registry.

    Args:
        db: Database session
        owner_id: User ID who owns the credentials
        agent_id: Optional agent ID for agent-level overrides

    Returns:
        Dictionary mapping connector type to status info. Format:
        {
            "github": {
                "status": "connected",
                "tools": ["github_create_issue", ...],
                "would_enable": ["Create issues", ...]
            },
            "slack": {
                "status": "not_configured",
                "setup_url": "/settings/integrations",
                "would_enable": ["Send messages to channels", ...]
            },
            ...
        }

    Status values:
        - "connected": Credentials configured (test_status == "success" or untested but present)
        - "not_configured": No credentials stored
        - "invalid_credentials": test_status == "failed"
    """
    # Create resolver to check configured connectors
    # For now, we use agent_id if provided, otherwise just owner_id
    # The resolver will handle the fallback logic
    effective_agent_id = agent_id or 0  # Use dummy agent_id if none provided
    resolver = CredentialResolver(
        agent_id=effective_agent_id,
        db=db,
        owner_id=owner_id,
    )

    # Get all configured connector types
    configured_types = resolver.get_all_configured()

    # Build status for all connectors in registry
    status_dict: dict[str, Any] = {}

    for connector_type in ConnectorType:
        connector_type_str = connector_type.value

        # Check if configured
        if connector_type_str in configured_types:
            # Connector is configured - need to determine if credentials are valid
            # For now, we treat any configured connector as "connected"
            # TODO: Check test_status field from database models once we query them
            # This would require fetching the actual credential records
            status_dict[connector_type_str] = {
                "status": "connected",
                "tools": get_tools_for_connector(connector_type),
                "would_enable": get_capabilities_for_connector(connector_type),
            }
        else:
            # Not configured
            status_dict[connector_type_str] = {
                "status": "not_configured",
                "setup_url": "/settings/integrations",
                "would_enable": get_capabilities_for_connector(connector_type),
            }

    logger.debug(
        "Built connector status for owner_id=%d agent_id=%s: %d configured",
        owner_id,
        agent_id,
        len(configured_types),
    )

    return status_dict


def build_agent_context(
    db: "Session",
    owner_id: int,
    agent_id: int | None = None,
) -> str:
    """Build the full context injection string for an agent turn.

    This function creates the XML-formatted context block that gets injected
    into every agent conversation turn, providing:
    - Current timestamp for temporal awareness
    - Connector status with captured_at timestamp

    Args:
        db: Database session
        owner_id: User ID who owns the credentials
        agent_id: Optional agent ID for agent-level overrides

    Returns:
        XML-formatted string with current_time and connector_status blocks

    Example output:
        <current_time>2025-01-17T15:00:00Z</current_time>

        <connector_status captured_at="2025-01-17T15:00:00Z">
        {
          "github": {
            "status": "connected",
            "tools": ["github_create_issue", ...],
            ...
          },
          ...
        }
        </connector_status>
    """
    # Get current time in UTC with Z suffix
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build connector status
    connector_status = build_connector_status(
        db=db,
        owner_id=owner_id,
        agent_id=agent_id,
    )

    # Format as XML with JSON inside
    context = f"""<current_time>{current_time}</current_time>

<connector_status captured_at="{current_time}">
{json.dumps(connector_status, indent=2)}
</connector_status>"""

    return context
