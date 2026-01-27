"""Slack-related tools for sending messages via webhooks."""

import logging
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import connector_not_configured_error
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)


def send_slack_webhook(
    text: str,
    webhook_url: Optional[str] = None,
    blocks: Optional[List[Dict[str, Any]]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
) -> Dict[str, Any]:
    """Send a message to Slack via an incoming webhook.

    This tool allows fiches to send rich, formatted messages to Slack channels.
    If Slack is configured in Fiche Settings, the webhook URL is used automatically.
    Otherwise, you must provide the webhook_url parameter.

    Rate Limits:
        - 1 request per second (short bursts allowed)
        - HTTP 429 responses include Retry-After header

    Args:
        text: Main message text (also used as fallback for notifications)
        webhook_url: Optional Slack webhook URL. If not provided, uses the
            webhook configured in Fiche Settings -> Connectors -> Slack.
        blocks: Optional list of Block Kit blocks for rich formatting.
            See https://api.slack.com/block-kit for block structure.
        attachments: Optional list of legacy attachments for additional content.
            Note: Blocks are preferred over attachments in modern Slack apps.
        unfurl_links: Whether to automatically unfurl links in the message (default: True)
        unfurl_media: Whether to automatically unfurl media in the message (default: True)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if message was sent successfully
        - status_code: HTTP status code from Slack
        - response: Response text from Slack (typically "ok" on success)
        - error: Error message if request failed (present only on failure)

    Example:
        # Simple text message (uses configured webhook)
        >>> send_slack_webhook(text="Hello from Zerg fiche!")
        {"success": True, "status_code": 200, "response": "ok"}

        # Rich message with blocks
        >>> blocks = [
        ...     {
        ...         "type": "section",
        ...         "text": {
        ...             "type": "mrkdwn",
        ...             "text": "*Deployment Status:* Success"
        ...         }
        ...     }
        ... ]
        >>> send_slack_webhook(text="Deployment completed", blocks=blocks)
        {"success": True, "status_code": 200, "response": "ok"}

    Error Codes:
        - 400: Invalid payload or malformed request
        - 403: Invalid webhook URL or webhook disabled
        - 404: Webhook URL not found
        - 429: Rate limit exceeded (check Retry-After header)
        - 500/502/503: Slack server errors
    """
    # Try to get webhook URL from context if not provided
    resolved_webhook_url = webhook_url
    if not resolved_webhook_url:
        resolver = get_credential_resolver()
        if resolver:
            creds = resolver.get(ConnectorType.SLACK)
            if creds:
                resolved_webhook_url = creds.get("webhook_url")

    # Validate inputs
    if not resolved_webhook_url or not resolved_webhook_url.strip():
        return connector_not_configured_error("slack", "Slack")

    if not text or not text.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="text cannot be empty",
            connector="slack",
        )

    # Validate webhook URL format
    if not resolved_webhook_url.startswith("https://hooks.slack.com/"):
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Invalid webhook URL format. Must start with 'https://hooks.slack.com/'",
            connector="slack",
        )

    # Build the payload
    payload = {
        "text": text,
        "unfurl_links": unfurl_links,
        "unfurl_media": unfurl_media,
    }

    # Validate and add blocks if provided
    if blocks is not None:
        if not isinstance(blocks, list):
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="blocks must be a list",
                connector="slack",
            )
        payload["blocks"] = blocks

    # Validate and add attachments if provided
    if attachments is not None:
        if not isinstance(attachments, list):
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="attachments must be a list",
                connector="slack",
            )
        payload["attachments"] = attachments

    # Send the webhook request
    try:
        with httpx.Client() as client:
            response = client.post(
                resolved_webhook_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Fiche": "Zerg-Fiche/1.0",
                },
                timeout=10.0,
            )

        # Check for success
        if response.status_code == 200:
            return tool_success(
                {
                    "status_code": response.status_code,
                    "response": response.text,
                }
            )

        # Handle error responses
        error_messages = {
            400: "Bad request - invalid payload or malformed request",
            403: "Forbidden - invalid webhook URL or webhook disabled",
            404: "Not found - webhook URL does not exist",
            429: "Rate limit exceeded - too many requests",
            500: "Slack server error - internal server error",
            502: "Slack server error - bad gateway",
            503: "Slack server error - service unavailable",
        }

        error_detail = error_messages.get(response.status_code, "Unknown error")
        user_message = error_detail

        # Add retry-after header if present (for 429 errors)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                user_message += f" (retry after {retry_after} seconds)"
            logger.warning(f"Slack webhook rate limited: {user_message}")
            return tool_error(
                error_type=ErrorType.RATE_LIMITED,
                user_message=user_message,
                connector="slack",
            )

        logger.warning(f"Slack webhook request failed: {user_message}")
        error_type = ErrorType.INVALID_CREDENTIALS if response.status_code in [403, 404] else ErrorType.EXECUTION_ERROR
        return tool_error(
            error_type=error_type,
            user_message=user_message,
            connector="slack",
        )

    except httpx.TimeoutException:
        logger.error("Slack webhook timeout")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Request timed out after 10 seconds",
            connector="slack",
        )
    except httpx.RequestError as e:
        logger.error(f"Slack webhook request error: {e}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Request failed: {str(e)}",
            connector="slack",
        )
    except Exception as e:
        logger.exception(f"Unexpected error sending Slack webhook: {e}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Unexpected error: {str(e)}",
            connector="slack",
        )


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=send_slack_webhook,
        name="send_slack_webhook",
        description=(
            "Send a message to Slack via incoming webhook. "
            "If Slack is configured in Fiche Settings -> Connectors, the webhook URL is used automatically. "
            "Supports simple text messages and rich formatting with Block Kit blocks. "
            "Use this to send notifications, alerts, or status updates to Slack channels."
        ),
    ),
]
