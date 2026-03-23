"""Webhook endpoints for channel plugins.

Provides a unified webhook endpoint that routes incoming webhooks to
the appropriate channel plugin based on the channel ID in the URL.

Example:
    POST /api/webhooks/channels/telegram
    -> Routes to TelegramChannel.handle_webhook()

Security:
- Each channel validates its own webhook signatures
- Request body size is clamped to prevent DoS
- Channels can reject invalid/malformed payloads
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import HTTPException
from fastapi import Request
from fastapi import status

from zerg.channels.registry import get_registry
from zerg.channels.sdk import WebhookChannel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["channel-webhooks"])

# Maximum body size for webhook payloads (128 KiB)
MAX_BODY_BYTES = 128 * 1024


async def _clamp_body_size(request: Request) -> None:
    """Reject requests with bodies larger than MAX_BODY_BYTES."""
    cl_header = request.headers.get("content-length")
    if cl_header and cl_header.isdigit():
        if int(cl_header) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        return

    # Read body and stash for later use
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    request.state.raw_body = raw  # type: ignore[attr-defined]


@router.post(
    "/webhooks/channels/{channel_id}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_clamp_body_size)],
)
async def channel_webhook(
    channel_id: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, Any]:
    """Handle incoming webhook for a channel plugin.

    Routes the webhook payload to the appropriate channel's handle_webhook method.
    The channel is responsible for validating signatures and processing the payload.

    Args:
        channel_id: The channel ID (e.g., "telegram", "discord")
        request: The incoming request
        x_telegram_bot_api_secret_token: Telegram's secret token header (optional)

    Returns:
        Response from the channel's webhook handler

    Raises:
        HTTPException: If channel not found, not a webhook channel, or validation fails
    """
    # Get channel from registry
    registry = get_registry()
    channel = registry.get(channel_id)

    if not channel:
        logger.warning(f"Webhook received for unknown channel: {channel_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel not found: {channel_id}",
        )

    # Verify channel supports webhooks
    if not isinstance(channel, WebhookChannel):
        logger.warning(f"Webhook received for non-webhook channel: {channel_id}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Channel {channel_id} does not support webhooks",
        )

    # Get raw body for signature validation
    raw_body: bytes
    if hasattr(request.state, "raw_body"):
        raw_body = request.state.raw_body
    else:
        raw_body = await request.body()

    # Parse JSON payload
    try:
        payload = await request.json()
    except Exception as e:
        logger.warning(f"Invalid JSON in webhook for {channel_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    # Validate webhook signature if channel requires it
    # For Telegram, use the X-Telegram-Bot-Api-Secret-Token header
    signature = x_telegram_bot_api_secret_token or ""
    if not channel.validate_webhook_signature(raw_body, signature):
        logger.warning(f"Invalid webhook signature for {channel_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # Handle the webhook
    try:
        result = await channel.handle_webhook(payload)
        return result or {"status": "ok"}

    except Exception as e:
        logger.exception(f"Error handling webhook for {channel_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Webhook processing error: {e}",
        )
