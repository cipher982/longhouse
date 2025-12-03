"""Connector status meta-tools for agent self-awareness.

This module provides tools that let agents query their own connector status,
enabling explicit verification of which integrations are available.
"""

from typing import Any

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.status_builder import build_connector_status
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success


def refresh_connector_status() -> dict[str, Any]:
    """Refresh and return the current connector status.

    Returns the latest status for all connectors, showing which integrations
    are connected, not configured, or have invalid credentials.

    This tool is useful when:
    - User explicitly asks to verify their connections
    - You want to confirm a connector is working before a critical action
    - User says "check if X is connected"

    Returns:
        dict: Success response with connector status data:
            {
                "ok": True,
                "data": {
                    "github": {"status": "connected", "tools": [...], ...},
                    "slack": {"status": "not_configured", "setup_url": "...", ...},
                    ...
                }
            }

        Or error response if context is unavailable:
            {
                "ok": False,
                "error_type": "execution_error",
                "user_message": "..."
            }
    """
    resolver = get_credential_resolver()
    if not resolver:
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            "Unable to check connector status - no credential context available.",
        )

    # Use resolver's db session and IDs to query fresh status
    try:
        status = build_connector_status(
            db=resolver.db,
            owner_id=resolver.owner_id,
            agent_id=resolver.agent_id,
        )
        return tool_success(status)
    except Exception as e:
        return tool_error(
            ErrorType.EXECUTION_ERROR,
            f"Failed to retrieve connector status: {e}",
        )


TOOLS = [
    StructuredTool.from_function(
        func=refresh_connector_status,
        name="refresh_connector_status",
        description="Check which connectors (GitHub, Slack, etc.) are currently connected and available. "
        "Use this to verify integrations before critical actions or when users ask about connection status.",
    ),
]
