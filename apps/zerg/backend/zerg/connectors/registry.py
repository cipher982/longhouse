"""Connector type registry defining metadata for each connector.

This module provides the ConnectorType enum and CONNECTOR_REGISTRY dictionary
that contains metadata for each built-in connector tool including:
- Display name and description
- Category (notifications vs project_management)
- Required credential fields with their types
- Documentation URLs for setup instructions
"""

from enum import Enum
from typing import List
from typing import TypedDict


class ConnectorType(str, Enum):
    """Enum of supported connector types."""

    SLACK = "slack"
    DISCORD = "discord"
    EMAIL = "email"
    SMS = "sms"
    GITHUB = "github"
    JIRA = "jira"
    LINEAR = "linear"
    NOTION = "notion"
    IMESSAGE = "imessage"
    SSH = "ssh"


class CredentialField(TypedDict):
    """Definition of a single credential field for a connector."""

    key: str  # Field key used in storage
    label: str  # Human-readable label
    type: str  # Input type: 'text', 'password', 'url'
    placeholder: str  # Example/hint for the field
    required: bool  # Whether the field is required


class ConnectorDefinition(TypedDict, total=False):
    """Full definition of a connector type."""

    type: ConnectorType
    name: str  # Display name
    description: str  # Short description
    category: str  # 'notifications', 'project_management', or 'infrastructure'
    icon: str  # Emoji icon
    docs_url: str  # URL to setup documentation
    fields: List[CredentialField]  # Required credential fields
    enabled_tools: List[str]  # Tools enabled by this connector (optional)


CONNECTOR_REGISTRY: dict[ConnectorType, ConnectorDefinition] = {
    ConnectorType.SLACK: {
        "type": ConnectorType.SLACK,
        "name": "Slack",
        "description": "Send messages to Slack channels via webhook",
        "category": "notifications",
        "icon": "slack",
        "docs_url": "https://api.slack.com/messaging/webhooks",
        "fields": [
            {
                "key": "webhook_url",
                "label": "Webhook URL",
                "type": "url",
                "placeholder": "https://hooks.slack.com/services/...",
                "required": True,
            }
        ],
        "enabled_tools": ["send_slack_message"],
    },
    ConnectorType.DISCORD: {
        "type": ConnectorType.DISCORD,
        "name": "Discord",
        "description": "Send messages to Discord channels via webhook",
        "category": "notifications",
        "icon": "discord",
        "docs_url": "https://discord.com/developers/docs/resources/webhook",
        "fields": [
            {
                "key": "webhook_url",
                "label": "Webhook URL",
                "type": "url",
                "placeholder": "https://discord.com/api/webhooks/...",
                "required": True,
            }
        ],
        "enabled_tools": ["send_discord_message"],
    },
    ConnectorType.EMAIL: {
        "type": ConnectorType.EMAIL,
        "name": "Email (Resend)",
        "description": "Send emails via Resend API",
        "category": "notifications",
        "icon": "mail",
        "docs_url": "https://resend.com/docs/api-reference/api-keys",
        "fields": [
            {
                "key": "api_key",
                "label": "API Key",
                "type": "password",
                "placeholder": "re_...",
                "required": True,
            },
            {
                "key": "from_email",
                "label": "From Email",
                "type": "text",
                "placeholder": "noreply@yourdomain.com",
                "required": True,
            },
        ],
        "enabled_tools": ["send_email"],
    },
    ConnectorType.SMS: {
        "type": ConnectorType.SMS,
        "name": "SMS (Twilio)",
        "description": "Send SMS messages via Twilio",
        "category": "notifications",
        "icon": "smartphone",
        "docs_url": "https://www.twilio.com/docs/usage/api",
        "fields": [
            {
                "key": "account_sid",
                "label": "Account SID",
                "type": "text",
                "placeholder": "AC...",
                "required": True,
            },
            {
                "key": "auth_token",
                "label": "Auth Token",
                "type": "password",
                "placeholder": "",
                "required": True,
            },
            {
                "key": "from_number",
                "label": "From Phone Number",
                "type": "text",
                "placeholder": "+1234567890",
                "required": True,
            },
        ],
        "enabled_tools": ["send_sms"],
    },
    ConnectorType.GITHUB: {
        "type": ConnectorType.GITHUB,
        "name": "GitHub",
        "description": "Create issues, PRs, and comments on GitHub",
        "category": "project_management",
        "icon": "github",
        "docs_url": "https://github.com/settings/tokens",
        "fields": [
            {
                "key": "token",
                "label": "Personal Access Token",
                "type": "password",
                "placeholder": "ghp_... or github_pat_...",
                "required": True,
            }
        ],
        "enabled_tools": [
            "github_create_issue",
            "github_list_issues",
            "github_get_issue",
            "github_add_comment",
            "github_list_pull_requests",
            "github_get_pull_request",
        ],
    },
    ConnectorType.JIRA: {
        "type": ConnectorType.JIRA,
        "name": "Jira",
        "description": "Create and manage Jira issues",
        "category": "project_management",
        "icon": "clipboard",
        "docs_url": "https://id.atlassian.com/manage-profile/security/api-tokens",
        "fields": [
            {
                "key": "domain",
                "label": "Jira Domain",
                "type": "text",
                "placeholder": "yourcompany.atlassian.net",
                "required": True,
            },
            {
                "key": "email",
                "label": "Email",
                "type": "text",
                "placeholder": "you@company.com",
                "required": True,
            },
            {
                "key": "api_token",
                "label": "API Token",
                "type": "password",
                "placeholder": "",
                "required": True,
            },
        ],
        "enabled_tools": [
            "jira_create_issue",
            "jira_list_issues",
            "jira_get_issue",
            "jira_add_comment",
            "jira_transition_issue",
            "jira_update_issue",
        ],
    },
    ConnectorType.LINEAR: {
        "type": ConnectorType.LINEAR,
        "name": "Linear",
        "description": "Create and manage Linear issues",
        "category": "project_management",
        "icon": "layout",
        "docs_url": "https://linear.app/settings/api",
        "fields": [
            {
                "key": "api_key",
                "label": "API Key",
                "type": "password",
                "placeholder": "lin_api_...",
                "required": True,
            }
        ],
        "enabled_tools": [
            "linear_create_issue",
            "linear_list_issues",
            "linear_get_issue",
            "linear_update_issue",
            "linear_add_comment",
            "linear_list_teams",
        ],
    },
    ConnectorType.NOTION: {
        "type": ConnectorType.NOTION,
        "name": "Notion",
        "description": "Create and manage Notion pages and databases",
        "category": "project_management",
        "icon": "file-text",
        "docs_url": "https://www.notion.so/my-integrations",
        "fields": [
            {
                "key": "api_key",
                "label": "Integration Token",
                "type": "password",
                "placeholder": "secret_... or ntn_...",
                "required": True,
            }
        ],
        "enabled_tools": [
            "notion_create_page",
            "notion_get_page",
            "notion_update_page",
            "notion_search",
            "notion_query_database",
            "notion_append_blocks",
        ],
    },
    ConnectorType.IMESSAGE: {
        "type": ConnectorType.IMESSAGE,
        "name": "iMessage",
        "description": "Send iMessages via macOS host (requires local setup)",
        "category": "notifications",
        "icon": "message-circle",
        "docs_url": "https://support.apple.com/messages",
        "fields": [
            {
                "key": "enabled",
                "label": "Enable iMessage",
                "type": "text",
                "placeholder": "true",
                "required": True,
            }
        ],
        "enabled_tools": ["send_imessage"],
    },
    ConnectorType.SSH: {
        "type": ConnectorType.SSH,
        "name": "SSH Target",
        "description": "Configure SSH targets for remote command execution via runners",
        "category": "infrastructure",
        "icon": "terminal",
        "docs_url": "https://docs.swarmlet.com/connectors/ssh",
        "fields": [
            {
                "key": "name",
                "label": "Target Name",
                "type": "text",
                "placeholder": "prod-web-1",
                "required": True,
            },
            {
                "key": "host",
                "label": "Host",
                "type": "text",
                "placeholder": "192.168.1.100 or server.example.com",
                "required": True,
            },
            {
                "key": "user",
                "label": "Username",
                "type": "text",
                "placeholder": "deploy (optional, uses runner's SSH config)",
                "required": False,
            },
            {
                "key": "port",
                "label": "Port",
                "type": "text",
                "placeholder": "22",
                "required": False,
            },
            {
                "key": "ssh_config_name",
                "label": "SSH Config Name",
                "type": "text",
                "placeholder": "Optional: use name from ~/.ssh/config",
                "required": False,
            },
        ],
        "enabled_tools": ["runner_ssh_exec", "ssh_target_list"],
    },
}


def get_connector_definition(connector_type: ConnectorType | str) -> ConnectorDefinition | None:
    """Get the definition for a connector type.

    Args:
        connector_type: ConnectorType enum or string value

    Returns:
        ConnectorDefinition if found, None otherwise
    """
    if isinstance(connector_type, str):
        try:
            connector_type = ConnectorType(connector_type)
        except ValueError:
            return None
    return CONNECTOR_REGISTRY.get(connector_type)


def get_required_fields(connector_type: ConnectorType | str) -> list[str]:
    """Get list of required field keys for a connector type.

    Args:
        connector_type: ConnectorType enum or string value

    Returns:
        List of required field keys, empty list if connector not found
    """
    definition = get_connector_definition(connector_type)
    if not definition:
        return []
    return [f["key"] for f in definition["fields"] if f["required"]]
