"""Discord webhook tools for sending messages to Discord channels."""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import (
    tool_error,
    tool_success,
    connector_not_configured_error,
    ErrorType,
)

logger = logging.getLogger(__name__)


def send_discord_webhook(
    content: Optional[str] = None,
    webhook_url: Optional[str] = None,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
    embeds: Optional[List[Dict[str, Any]]] = None,
    tts: bool = False,
) -> Dict[str, Any]:
    """Send a message to Discord via webhook.

    Discord webhooks allow sending rich messages to channels without requiring bot authentication.
    Messages can include simple text content, custom usernames/avatars, and rich embeds with
    formatting, images, fields, and more.

    Rate limit: 5 requests per second per webhook. This function does not implement automatic
    retry logic for rate limits - the caller should handle 429 responses if needed.

    Args:
        content: Message text content (max 2000 characters). Optional if embeds are provided.
        webhook_url: Discord webhook URL in format:
            https://discord.com/api/webhooks/{webhook.id}/{webhook.token}
            Optional - uses configured credentials from Agent Settings if not provided.
        username: Override the webhook's default username (optional)
        avatar_url: Override the webhook's default avatar (optional)
        embeds: List of embed objects for rich content (max 10 embeds). See below for structure.
        tts: Whether to send message as text-to-speech (default: False)

    Embed Structure:
        Each embed can contain:
        - title: Embed title (max 256 chars)
        - description: Main content (max 4096 chars)
        - url: URL to associate with the title
        - color: Sidebar color as decimal integer (e.g., 0xFF0000 = 16711680 for red)
        - fields: List of field objects (max 25), each with:
            - name: Field name (max 256 chars)
            - value: Field value (max 1024 chars)
            - inline: Whether to display inline (boolean)
        - author: Object with name, url, icon_url
        - footer: Object with text (max 2048 chars), icon_url
        - image: Object with url for large image
        - thumbnail: Object with url for thumbnail
        - timestamp: ISO 8601 timestamp string

        Total characters across all embed fields cannot exceed 6000.

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the message was sent
        - status_code: HTTP status code (204 for success, 429 for rate limit, etc.)
        - error: Error message if request failed
        - rate_limit_retry_after: Seconds to wait if rate limited (429 response)

    Example:
        >>> # Simple text message
        >>> send_discord_webhook(
        ...     webhook_url="https://discord.com/api/webhooks/123/abc",
        ...     content="Agent task completed successfully!"
        ... )
        {"success": True, "status_code": 204}

        >>> # Rich message with embed
        >>> send_discord_webhook(
        ...     webhook_url="https://discord.com/api/webhooks/123/abc",
        ...     content="Status Update",
        ...     username="Zerg Agent",
        ...     embeds=[{
        ...         "title": "Task Complete",
        ...         "description": "All operations finished",
        ...         "color": 3066993,  # Green
        ...         "fields": [
        ...             {"name": "Duration", "value": "2.5s", "inline": True},
        ...             {"name": "Status", "value": "Success", "inline": True}
        ...         ]
        ...     }]
        ... )
        {"success": True, "status_code": 204}
    """
    try:
        # Try to get webhook URL from context if not provided
        resolved_webhook_url = webhook_url
        if not resolved_webhook_url:
            resolver = get_credential_resolver()
            if resolver:
                creds = resolver.get(ConnectorType.DISCORD)
                if creds:
                    resolved_webhook_url = creds.get("webhook_url")

        # Validate webhook URL format
        if not resolved_webhook_url or not resolved_webhook_url.startswith("https://discord.com/api/webhooks/"):
            return connector_not_configured_error("discord", "Discord")

        # Validate that we have at least content or embeds
        if not content and not embeds:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Must provide either 'content' or 'embeds' (or both)",
                connector="discord",
            )

        # Build the payload
        payload = {}

        if content:
            if len(content) > 2000:
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message="Content exceeds 2000 character limit",
                    connector="discord",
                )
            payload["content"] = content

        if username:
            payload["username"] = username

        if avatar_url:
            payload["avatar_url"] = avatar_url

        if embeds:
            if not isinstance(embeds, list):
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message="Embeds must be a list",
                    connector="discord",
                )

            if len(embeds) > 10:
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message="Maximum of 10 embeds allowed per message",
                    connector="discord",
                )

            # Basic validation of embed structure
            for idx, embed in enumerate(embeds):
                if not isinstance(embed, dict):
                    return tool_error(
                        error_type=ErrorType.VALIDATION_ERROR,
                        user_message=f"Embed {idx} must be a dictionary",
                        connector="discord",
                    )

                # Validate common length limits
                if "title" in embed and len(embed["title"]) > 256:
                    return tool_error(
                        error_type=ErrorType.VALIDATION_ERROR,
                        user_message=f"Embed {idx} title exceeds 256 character limit",
                        connector="discord",
                    )

                if "description" in embed and len(embed["description"]) > 4096:
                    return tool_error(
                        error_type=ErrorType.VALIDATION_ERROR,
                        user_message=f"Embed {idx} description exceeds 4096 character limit",
                        connector="discord",
                    )

            payload["embeds"] = embeds

        if tts:
            payload["tts"] = tts

        # Make the request
        with httpx.Client() as client:
            response = client.post(
                resolved_webhook_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Zerg-Agent/1.0",
                },
                timeout=30.0,
            )

        # Discord webhooks return 204 No Content on success
        if response.status_code == 204:
            logger.info(f"Discord webhook message sent successfully")
            return tool_success({"status_code": 204})

        # Handle rate limiting (429 Too Many Requests)
        if response.status_code == 429:
            try:
                rate_limit_data = response.json()
                retry_after = rate_limit_data.get("retry_after", 0)
                logger.warning(f"Discord webhook rate limited. Retry after {retry_after}s")
                return tool_error(
                    error_type=ErrorType.RATE_LIMITED,
                    user_message=f"Rate limit exceeded. Retry after {retry_after} seconds",
                    connector="discord",
                )
            except json.JSONDecodeError:
                return tool_error(
                    error_type=ErrorType.RATE_LIMITED,
                    user_message="Rate limit exceeded (could not parse retry time)",
                    connector="discord",
                )

        # Handle authentication errors (invalid webhook)
        if response.status_code in [401, 403, 404]:
            try:
                error_data = response.json()
                error_message = error_data.get("message", response.text)
            except json.JSONDecodeError:
                error_message = response.text

            logger.error(f"Discord webhook invalid: {response.status_code} - {error_message}")
            return tool_error(
                error_type=ErrorType.INVALID_CREDENTIALS,
                user_message=f"Invalid or disabled webhook URL: {error_message}",
                connector="discord",
            )

        # Handle other error responses
        try:
            error_data = response.json()
            error_message = error_data.get("message", response.text)
        except json.JSONDecodeError:
            error_message = response.text

        logger.error(f"Discord webhook failed: {response.status_code} - {error_message}")
        error_type = ErrorType.VALIDATION_ERROR if response.status_code == 400 else ErrorType.EXECUTION_ERROR
        return tool_error(
            error_type=error_type,
            user_message=f"Discord API error: {error_message}",
            connector="discord",
        )

    except httpx.TimeoutException:
        logger.error(f"Discord webhook timeout for URL: {resolved_webhook_url}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Request timed out after 30 seconds",
            connector="discord",
        )
    except httpx.RequestError as e:
        logger.error(f"Discord webhook request error: {e}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Request failed: {str(e)}",
            connector="discord",
        )
    except Exception as e:
        logger.exception(f"Unexpected error in send_discord_webhook")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Unexpected error: {str(e)}",
            connector="discord",
        )


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=send_discord_webhook,
        name="send_discord_webhook",
        description="Send a message to Discord via webhook. Supports text content, custom usernames/avatars, and rich embeds with formatting, images, and fields. Uses webhook URL from Agent Settings -> Connectors if not explicitly provided.",
    ),
]
