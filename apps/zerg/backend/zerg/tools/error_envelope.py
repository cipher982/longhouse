"""Standardized error envelope for tool responses.

All tools should use these helpers to return consistent error/success responses
that the agent can interpret using the <error_handling> protocol.
"""
from enum import Enum
from typing import Any
from typing import TypedDict


class ErrorType(str, Enum):
    """Standard error types for tool failures."""
    CONNECTOR_NOT_CONFIGURED = "connector_not_configured"
    INVALID_CREDENTIALS = "invalid_credentials"
    RATE_LIMITED = "rate_limited"
    PERMISSION_DENIED = "permission_denied"
    VALIDATION_ERROR = "validation_error"
    EXECUTION_ERROR = "execution_error"


class ToolErrorResponse(TypedDict, total=False):
    """Structured error response from a tool."""
    ok: bool
    error_type: str
    user_message: str
    connector: str | None
    setup_url: str | None


class ToolSuccessResponse(TypedDict):
    """Structured success response from a tool."""
    ok: bool
    data: Any


def tool_error(
    error_type: ErrorType,
    user_message: str,
    connector: str | None = None,
    setup_url: str | None = None,
) -> ToolErrorResponse:
    """Create a standardized error response."""
    response: ToolErrorResponse = {
        "ok": False,
        "error_type": error_type.value,
        "user_message": user_message,
    }
    if connector:
        response["connector"] = connector
    if setup_url:
        response["setup_url"] = setup_url
    return response


def tool_success(data: Any) -> ToolSuccessResponse:
    """Create a standardized success response."""
    return {
        "ok": True,
        "data": data,
    }


# Convenience functions for common connector errors
def connector_not_configured_error(
    connector: str,
    connector_display_name: str | None = None,
) -> ToolErrorResponse:
    """Error when a connector is not configured."""
    name = connector_display_name or connector.title()
    return tool_error(
        error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
        user_message=f"{name} is not connected. Set it up in Settings → Integrations → {name}.",
        connector=connector,
        setup_url="/settings/integrations",
    )


def invalid_credentials_error(
    connector: str,
    connector_display_name: str | None = None,
) -> ToolErrorResponse:
    """Error when connector credentials are invalid/expired."""
    name = connector_display_name or connector.title()
    return tool_error(
        error_type=ErrorType.INVALID_CREDENTIALS,
        user_message=f"{name} credentials have expired. Please reconnect in Settings → Integrations.",
        connector=connector,
        setup_url="/settings/integrations",
    )
