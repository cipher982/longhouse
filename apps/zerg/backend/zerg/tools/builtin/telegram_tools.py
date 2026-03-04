"""Telegram notification tool — lets Oikos proactively message the user on Telegram."""

from __future__ import annotations

import logging
from typing import Any
from typing import Dict

from zerg.context import get_commis_context
from zerg.crud import crud
from zerg.database import db_session
from zerg.services.telegram_bridge import _format_for_telegram
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success
from zerg.types.tools import Tool as StructuredTool

logger = logging.getLogger(__name__)


def send_telegram(message: str) -> Dict[str, Any]:
    """Send a Telegram message to the user.

    Use this to proactively reach the user on their phone — e.g. when a long
    task finishes, when you need their attention, or when they asked you to
    follow up later.

    Only works if the user has previously messaged the Longhouse Telegram bot
    (their chat_id must be stored).

    Args:
        message: The message to send. Supports markdown (bold, italic, code blocks).

    Returns:
        {"success": True} or {"success": False, "error": "..."}

    Example:
        >>> send_telegram("✅ Backup complete — 1.2 GB uploaded to S3 in 4m 32s.")
        {"success": True}
    """
    if not message or not message.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="message is required",
        )

    # Get owner from commis context
    ctx = get_commis_context()
    owner_id = ctx.owner_id if ctx else None

    if owner_id is None:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="send_telegram requires a commis context with owner information.",
        )

    # Look up the user's telegram_chat_id
    with db_session() as db:
        user = crud.get_user(db, owner_id)
        if not user:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message=f"User {owner_id} not found.",
            )
        chat_id = str((user.context or {}).get("telegram_chat_id", ""))

    if not chat_id:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=("No Telegram chat linked. The user needs to message @Longhouse_drose_bot first " "to establish the connection."),
        )

    # Send via the registered TelegramChannel
    from zerg.channels.registry import get_registry

    registry = get_registry()
    channel = registry.get("telegram")
    if not channel:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Telegram channel is not active on this instance.",
        )

    import asyncio

    from zerg.channels.types import ChannelMessage

    msg: ChannelMessage = {
        "channel_id": "telegram",
        "to": chat_id,
        "text": _format_for_telegram(message),
        "parse_mode": "html",
    }

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context — schedule and wait
            future = asyncio.run_coroutine_threadsafe(channel.send_message(msg), loop)
            result = future.result(timeout=10)
        else:
            result = loop.run_until_complete(channel.send_message(msg))
    except Exception as e:
        logger.exception("send_telegram: delivery failed for chat %s: %s", chat_id, e)
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to send Telegram message: {e}",
        )

    if not result.get("success"):
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Telegram delivery failed: {result.get('error')} (code={result.get('error_code')})",
        )

    logger.info("send_telegram: delivered to chat %s", chat_id)
    return tool_success({"delivered_to": chat_id})


send_telegram_tool = StructuredTool(
    func=send_telegram,
    name="send_telegram",
    description=(
        "Send a Telegram message to the user on their phone. "
        "Use when a task finishes, you need to flag something urgent, or the user asked you to follow up. "
        "Only works after the user has messaged the bot at least once."
    ),
)
