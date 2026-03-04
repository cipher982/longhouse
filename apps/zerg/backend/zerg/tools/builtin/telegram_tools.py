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

_DESCRIPTION = (
    "Send a Telegram message to the user on their phone. "
    "Use when a task finishes, you need to flag something urgent, or the user asked you to follow up. "
    "Only works after the user has messaged the bot at least once."
)


def _resolve_chat_id() -> tuple[str | None, Dict[str, Any] | None]:
    """Return (chat_id, None) or (None, error_dict)."""
    ctx = get_commis_context()
    owner_id = ctx.owner_id if ctx else None
    if owner_id is None:
        return None, tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="send_telegram requires a commis context with owner information.",
        )

    with db_session() as db:
        user = crud.get_user(db, owner_id)
        if not user:
            return None, tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message=f"User {owner_id} not found.",
            )
        chat_id = str((user.context or {}).get("telegram_chat_id", ""))

    if not chat_id:
        return None, tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=("No Telegram chat linked. The user needs to message the Longhouse bot first " "to establish the connection."),
        )
    return chat_id, None


def _build_message(chat_id: str, text: str) -> Any:
    from zerg.channels.types import ChannelMessage

    return ChannelMessage(
        channel_id="telegram",
        to=chat_id,
        text=_format_for_telegram(text),
        parse_mode="html",
    )


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
        return tool_error(error_type=ErrorType.VALIDATION_ERROR, user_message="message is required")

    chat_id, err = _resolve_chat_id()
    if err:
        return err

    from zerg.channels.registry import get_registry

    channel = get_registry().get("telegram")
    if not channel:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Telegram channel is not active on this instance.",
        )

    import asyncio

    msg = _build_message(chat_id, message)  # type: ignore[arg-type]
    try:
        result = asyncio.run_coroutine_threadsafe(channel.send_message(msg), asyncio.get_running_loop()).result(timeout=10)
    except RuntimeError:
        # No running loop — called from a truly sync context (e.g. tests)
        result = asyncio.run(channel.send_message(msg))
    except Exception as e:
        logger.exception("send_telegram: delivery failed for chat %s", chat_id)
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Failed to send: {e}")

    if not result.get("success"):
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Telegram delivery failed: {result.get('error')} (code={result.get('error_code')})",
        )

    logger.info("send_telegram: delivered to chat %s", chat_id)
    return tool_success({"delivered_to": chat_id})


async def send_telegram_async(message: str) -> Dict[str, Any]:
    """Async version — used by Oikos ainvoke path (avoids thread/loop bridging entirely)."""
    if not message or not message.strip():
        return tool_error(error_type=ErrorType.VALIDATION_ERROR, user_message="message is required")

    chat_id, err = _resolve_chat_id()
    if err:
        return err

    from zerg.channels.registry import get_registry

    channel = get_registry().get("telegram")
    if not channel:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Telegram channel is not active on this instance.",
        )

    msg = _build_message(chat_id, message)  # type: ignore[arg-type]
    try:
        result = await channel.send_message(msg)
    except Exception as e:
        logger.exception("send_telegram: delivery failed for chat %s", chat_id)
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Failed to send: {e}")

    if not result.get("success"):
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Telegram delivery failed: {result.get('error')} (code={result.get('error_code')})",
        )

    logger.info("send_telegram: delivered to chat %s", chat_id)
    return tool_success({"delivered_to": chat_id})


send_telegram_tool = StructuredTool.from_function(
    func=send_telegram,
    coroutine=send_telegram_async,
    name="send_telegram",
    description=_DESCRIPTION,
)
