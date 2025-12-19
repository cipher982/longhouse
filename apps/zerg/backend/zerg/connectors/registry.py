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
    # Personal integrations (Phase 4 v2.1)
    TRACCAR = "traccar"
    WHOOP = "whoop"
    OBSIDIAN = "obsidian"


class CredentialField(TypedDict):
    """Definition of a single credential field for a connector."""

    key: str  # Field key used in storage
    label: str  # Human-readable label
    type: str  # Input type: 'text', 'password', 'url'
    placeholder: str  # Example/hint for the field
    required: bool  # Whether the field is required


class ConnectorDefinition(TypedDict):
    """Full definition of a connector type."""

    type: ConnectorType
    name: str  # Display name
    description: str  # Short description
    category: str  # 'notifications' or 'project_management'
    icon: str  # Emoji icon
    docs_url: str  # URL to setup documentation
    fields: List[CredentialField]  # Required credential fields


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
    },
    # Personal integrations (Phase 4 v2.1)
    ConnectorType.TRACCAR: {
        "type": ConnectorType.TRACCAR,
        "name": "Traccar",
        "description": "Get GPS location from Traccar tracking server",
        "category": "personal",
        "icon": "map-pin",
        "docs_url": "https://www.traccar.org/api-reference/",
        "fields": [
            {
                "key": "url",
                "label": "Traccar Server URL",
                "type": "url",
                "placeholder": "http://traccar.example.com:5055",
                "required": True,
            },
            {
                "key": "username",
                "label": "Username",
                "type": "text",
                "placeholder": "admin",
                "required": True,
            },
            {
                "key": "password",
                "label": "Password",
                "type": "password",
                "placeholder": "Your Traccar password",
                "required": True,
            },
            {
                "key": "device_id",
                "label": "Device ID",
                "type": "text",
                "placeholder": "1",
                "required": False,
            },
        ],
    },
    ConnectorType.WHOOP: {
        "type": ConnectorType.WHOOP,
        "name": "WHOOP",
        "description": "Get health metrics from WHOOP fitness tracker",
        "category": "personal",
        "icon": "activity",
        "docs_url": "https://developer.whoop.com/docs/introduction",
        "fields": [
            {
                "key": "client_id",
                "label": "OAuth Client ID",
                "type": "text",
                "placeholder": "From WHOOP Developer Portal",
                "required": True,
            },
            {
                "key": "client_secret",
                "label": "OAuth Client Secret",
                "type": "password",
                "placeholder": "From WHOOP Developer Portal",
                "required": True,
            },
            {
                "key": "access_token",
                "label": "Access Token",
                "type": "password",
                "placeholder": "OAuth access token",
                "required": True,
            },
            {
                "key": "refresh_token",
                "label": "Refresh Token",
                "type": "password",
                "placeholder": "OAuth refresh token (enables auto-refresh)",
                "required": False,
            },
        ],
    },
    ConnectorType.OBSIDIAN: {
        "type": ConnectorType.OBSIDIAN,
        "name": "Obsidian",
        "description": "Search notes in Obsidian vault (via Runner)",
        "category": "personal",
        "icon": "book-open",
        "docs_url": "https://obsidian.md/",
        "fields": [
            {
                "key": "vault_path",
                "label": "Vault Path",
                "type": "text",
                "placeholder": "~/obsidian_vault",
                "required": True,
            },
            {
                "key": "runner_name",
                "label": "Runner Name",
                "type": "text",
                "placeholder": "laptop",
                "required": True,
            },
        ],
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
