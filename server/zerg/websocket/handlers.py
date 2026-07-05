"""WebSocket message handlers for topic-based browser subscriptions."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError
from sqlalchemy.orm import Session

from zerg.crud.crud_users import get_user
from zerg.generated.ws_messages import Envelope
from zerg.generated.ws_messages import ErrorData
from zerg.generated.ws_messages import MessageType
from zerg.generated.ws_messages import PingData
from zerg.generated.ws_messages import PongData
from zerg.generated.ws_messages import SubscribeData
from zerg.generated.ws_messages import UnsubscribeData
from zerg.generated.ws_messages import UserUpdateData
from zerg.websocket.manager import topic_manager
from zerg.websocket.subscription_helpers import send_subscribe_ack
from zerg.websocket.subscription_helpers import send_subscribe_error
from zerg.websocket.subscription_helpers import subscribe_and_send_state

logger = logging.getLogger(__name__)


async def send_to_client(
    client_id: str,
    message: dict[str, Any],
    *,
    topic: str | None = None,
) -> bool:
    """Send an envelope-format message to a connected client."""
    _ = topic
    if client_id not in topic_manager.active_connections:
        return False
    try:
        await topic_manager.active_connections[client_id].send_json(message)  # type: ignore[arg-type]
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Error sending to client %s: %s", client_id, exc)
        return False


async def send_error(
    client_id: str,
    error_msg: str,
    message_id: str | None = None,
    *,
    close_code: int | None = None,
) -> None:
    error_data = ErrorData(
        error=error_msg,
        details={"message_id": message_id} if message_id else None,
    )
    envelope = Envelope.create(
        message_type=MessageType.ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=message_id,
    )
    await send_to_client(client_id, envelope.model_dump())

    if close_code is not None and client_id in topic_manager.active_connections:
        try:
            await topic_manager.active_connections[client_id].close(code=close_code)
        except Exception:  # noqa: BLE001
            pass


async def handle_user_subscription(client_id: str, user_id: int, message_id: str, db: Session) -> None:
    """Subscribe to user profile updates."""
    topic = f"user:{user_id}"
    if user_id <= 0:
        await topic_manager.subscribe_to_topic(client_id, topic)
        await send_subscribe_ack(client_id, message_id, [topic], send_to_client)
        return

    user = get_user(db, user_id)
    if not user:
        await send_subscribe_error(client_id, message_id, f"User {user_id} not found", [topic], send_to_client, "NOT_FOUND")
        return

    user_data = UserUpdateData(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )
    await subscribe_and_send_state(client_id, topic, message_id, user_data, "user_update", send_to_client)


async def handle_ops_subscription(client_id: str, message_id: str, db: Session) -> None:
    """Subscribe to ops events (admin-only)."""
    topic = "ops:events"
    user_id = topic_manager.client_users.get(client_id)
    if not user_id:
        await send_subscribe_error(client_id, message_id, "Unauthorized", [topic], send_to_client, "UNAUTHORIZED")
        return

    user = get_user(db, int(user_id))
    if not user or getattr(user, "role", "USER") != "ADMIN":
        await send_subscribe_error(client_id, message_id, "Admin privileges required", [topic], send_to_client, "FORBIDDEN")
        return

    await topic_manager.subscribe_to_topic(client_id, topic)
    await send_subscribe_ack(client_id, message_id, [topic], send_to_client)


async def handle_ping(client_id: str, envelope: Envelope, _: Session) -> None:
    """Handle ping messages to keep connection alive."""
    try:
        ping_data = PingData.model_validate(envelope.data)
        pong_data = PongData(timestamp=ping_data.timestamp)
        response_envelope = Envelope.create(
            message_type="pong",
            topic="system",
            data=pong_data.model_dump(),
            req_id=envelope.req_id,
        )
        await send_to_client(client_id, response_envelope.model_dump())
    except Exception as exc:
        logger.error("Error handling ping: %s", exc)
        await send_error(client_id, "Failed to process ping")


async def handle_pong(client_id: str, envelope: Envelope, _: Session) -> None:
    """Handle pong frames sent by clients."""
    _ = envelope
    try:
        topic_manager.record_pong(client_id)
    except Exception as exc:
        logger.debug("Failed to record pong from %s: %s", client_id, exc)


async def handle_subscribe(client_id: str, envelope: Envelope, db: Session) -> None:
    """Handle topic subscription requests."""
    try:
        subscribe_data = SubscribeData.model_validate(envelope.data)
        message_id = subscribe_data.message_id or envelope.req_id or "unknown"

        for topic in subscribe_data.topics:
            try:
                prefix, topic_id = topic.split(":", 1)
                if prefix == "user":
                    await handle_user_subscription(client_id, int(topic_id), message_id, db)
                elif prefix == "ops" and topic_id == "events":
                    await handle_ops_subscription(client_id, message_id, db)
                else:
                    await send_subscribe_error(
                        client_id, message_id, f"Unsupported topic type: {prefix}", [topic], send_to_client, "UNSUPPORTED"
                    )
            except (ValueError, IndexError):
                await send_subscribe_error(
                    client_id, message_id, f"Invalid topic format: {topic}", [topic], send_to_client, "INVALID_FORMAT"
                )

    except ValidationError as exc:
        logger.error("Invalid subscription data: %s", exc)
        await send_error(client_id, "Invalid subscription format", envelope.req_id)
    except Exception as exc:
        logger.error("Error handling subscription: %s", exc)
        await send_error(client_id, "Failed to process subscription", envelope.req_id)


async def handle_unsubscribe(client_id: str, envelope: Envelope, _: Session) -> None:
    """Handle topic unsubscription requests."""
    try:
        unsub_data = UnsubscribeData.model_validate(envelope.data)
        message_id = unsub_data.message_id or envelope.req_id or ""
        for topic in unsub_data.topics:
            await topic_manager.unsubscribe_from_topic(client_id, topic)
        ack_envelope = Envelope.create(
            message_type="unsubscribe_success",
            topic="system",
            data={"message_id": message_id, "topics": unsub_data.topics},
            req_id=message_id,
        )
        await send_to_client(client_id, ack_envelope.model_dump())
    except Exception as exc:
        logger.error("Error handling unsubscribe: %s", exc)
        await send_error(client_id, "Failed to process unsubscribe", envelope.req_id)


MESSAGE_HANDLERS = {
    "ping": handle_ping,
    "pong": handle_pong,
    "subscribe": handle_subscribe,
    "unsubscribe": handle_unsubscribe,
}

_INBOUND_SCHEMA_MAP: dict[str, type[BaseModel]] = {
    "ping": PingData,
    "pong": PongData,
    "subscribe": SubscribeData,
    "unsubscribe": UnsubscribeData,
}


async def dispatch_message(client_id: str, message: dict[str, Any], db: Session) -> None:
    """Dispatch an envelope-format message to the appropriate handler."""
    try:
        try:
            envelope = Envelope.model_validate(message)
        except ValidationError:
            await send_error(client_id, "INVALID_ENVELOPE: all messages must use envelope format", close_code=1002)
            return

        message_type = envelope.type
        if message_type not in MESSAGE_HANDLERS:
            await send_error(client_id, f"Unknown message type: {message_type}", envelope.req_id)
            return

        model_cls = _INBOUND_SCHEMA_MAP.get(message_type)
        if model_cls is not None:
            try:
                model_cls.model_validate(envelope.data)
            except ValidationError:
                await send_error(client_id, "INVALID_PAYLOAD", envelope.req_id, close_code=1002)
                return

        await MESSAGE_HANDLERS[message_type](client_id, envelope, db)

    except Exception as exc:
        logger.error("Error dispatching message: %s", exc)
        await send_error(client_id, "Failed to process message")


__all__ = ["dispatch_message"]
