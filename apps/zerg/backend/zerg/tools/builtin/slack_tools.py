"""Slack-related tools for sending messages via webhooks."""

import logging
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


def send_slack_webhook(
    webhook_url: str,
    text: str,
    blocks: Optional[List[Dict[str, Any]]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
) -> Dict[str, Any]:
    """Send a message to Slack via an incoming webhook.

    This tool allows agents to send rich, formatted messages to Slack channels.
    Slack webhooks support simple text messages as well as advanced formatting
    using Block Kit blocks and legacy attachments.

    Rate Limits:
        - 1 request per second (short bursts allowed)
        - HTTP 429 responses include Retry-After header

    Args:
        webhook_url: The Slack webhook URL (format: https://hooks.slack.com/services/...)
        text: Main message text (also used as fallback for notifications)
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
        # Simple text message
        >>> send_slack_webhook(
        ...     webhook_url="https://hooks.slack.com/services/YOUR/WEBHOOK/URL",
        ...     text="Hello from Zerg agent!"
        ... )
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
        >>> send_slack_webhook(
        ...     webhook_url="https://hooks.slack.com/services/YOUR/WEBHOOK/URL",
        ...     text="Deployment completed",
        ...     blocks=blocks
        ... )
        {"success": True, "status_code": 200, "response": "ok"}

    Error Codes:
        - 400: Invalid payload or malformed request
        - 403: Invalid webhook URL or webhook disabled
        - 404: Webhook URL not found
        - 429: Rate limit exceeded (check Retry-After header)
        - 500/502/503: Slack server errors
    """
    # Validate inputs
    if not webhook_url or not webhook_url.strip():
        return {
            "success": False,
            "status_code": 0,
            "error": "webhook_url cannot be empty",
        }

    if not text or not text.strip():
        return {
            "success": False,
            "status_code": 0,
            "error": "text cannot be empty",
        }

    # Validate webhook URL format
    if not webhook_url.startswith("https://hooks.slack.com/"):
        return {
            "success": False,
            "status_code": 0,
            "error": "Invalid webhook URL format. Must start with 'https://hooks.slack.com/'",
        }

    # Build the payload
    payload = {
        "text": text,
        "unfurl_links": unfurl_links,
        "unfurl_media": unfurl_media,
    }

    # Validate and add blocks if provided
    if blocks is not None:
        if not isinstance(blocks, list):
            return {
                "success": False,
                "status_code": 0,
                "error": "blocks must be a list",
            }
        payload["blocks"] = blocks

    # Validate and add attachments if provided
    if attachments is not None:
        if not isinstance(attachments, list):
            return {
                "success": False,
                "status_code": 0,
                "error": "attachments must be a list",
            }
        payload["attachments"] = attachments

    # Send the webhook request
    try:
        with httpx.Client() as client:
            response = client.post(
                webhook_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Zerg-Agent/1.0",
                },
                timeout=10.0,
            )

        # Check for success
        if response.status_code == 200:
            return {
                "success": True,
                "status_code": response.status_code,
                "response": response.text,
            }

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
        result = {
            "success": False,
            "status_code": response.status_code,
            "error": f"{error_detail}",
            "response": response.text[:200] if response.text else "",
        }

        # Add retry-after header if present (for 429 errors)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                result["retry_after_seconds"] = retry_after
                result["error"] += f" (retry after {retry_after} seconds)"

        logger.warning(f"Slack webhook request failed: {result['error']}")
        return result

    except httpx.TimeoutException:
        logger.error(f"Slack webhook timeout for URL: {webhook_url}")
        return {
            "success": False,
            "status_code": 0,
            "error": "Request timed out after 10 seconds",
        }
    except httpx.RequestError as e:
        logger.error(f"Slack webhook request error: {e}")
        return {
            "success": False,
            "status_code": 0,
            "error": f"Request failed: {str(e)}",
        }
    except Exception as e:
        logger.exception(f"Unexpected error sending Slack webhook: {e}")
        return {
            "success": False,
            "status_code": 0,
            "error": f"Unexpected error: {str(e)}",
        }


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=send_slack_webhook,
        name="send_slack_webhook",
        description=(
            "Send a message to Slack via incoming webhook. "
            "Supports simple text messages and rich formatting with Block Kit blocks. "
            "Use this to send notifications, alerts, or status updates to Slack channels."
        ),
    ),
]
