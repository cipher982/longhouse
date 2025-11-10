"""Simple helpers for subscription handlers.

For a solo dev with single consumer, we don't need decorators and context objects.
Just clean helper functions that send acks and errors consistently.
"""

import logging
from typing import Optional

from pydantic import BaseModel

from zerg.generated.ws_messages import Envelope, SubscribeAckData, SubscribeErrorData
from zerg.websocket.manager import topic_manager

logger = logging.getLogger(__name__)


async def send_subscribe_ack(
    client_id: str,
    message_id: str,
    topics: list[str],
    send_to_client_func,
) -> None:
    """Send subscription acknowledgment.

    Args:
        client_id: WebSocket client ID
        message_id: Message ID for correlation
        topics: List of successfully subscribed topics
        send_to_client_func: Function to send message (passed to avoid circular import)
    """
    ack_data = SubscribeAckData(message_id=message_id, topics=topics)
    ack_envelope = Envelope.create(
        message_type="subscribe_ack",
        topic="system",
        data=ack_data.model_dump(),
        req_id=message_id,
    )
    await send_to_client_func(client_id, ack_envelope.model_dump(), topic="system")
    logger.debug(f"Sent subscribe_ack for topics {topics} to client {client_id}")


async def send_subscribe_error(
    client_id: str,
    message_id: str,
    error: str,
    topics: list[str],
    send_to_client_func,
    error_code: Optional[str] = None,
) -> None:
    """Send subscription error.

    Args:
        client_id: WebSocket client ID
        message_id: Message ID for correlation
        error: Human-readable error message
        topics: List of topics that failed
        send_to_client_func: Function to send message
        error_code: Optional machine-readable error code
    """
    error_data = SubscribeErrorData(
        message_id=message_id,
        topics=topics,
        error=error,
        error_code=error_code,
    )
    error_envelope = Envelope.create(
        message_type="subscribe_error",
        topic="system",
        data=error_data.model_dump(),
        req_id=message_id,
    )
    await send_to_client_func(client_id, error_envelope.model_dump(), topic="system")
    logger.warning(f"Subscription error for {topics}: {error} (code: {error_code})")


async def subscribe_and_send_state(
    client_id: str,
    topic: str,
    message_id: str,
    initial_state: BaseModel,
    message_type: str,
    send_to_client_func,
) -> None:
    """Subscribe to topic and send initial state + ack.

    Common pattern: subscribe → send state → send ack.

    Args:
        client_id: WebSocket client ID
        topic: Topic to subscribe to
        message_id: Message ID for correlation
        initial_state: Pydantic model with initial state
        message_type: Message type for initial state (e.g., "agent_state")
        send_to_client_func: Function to send message
    """
    # Subscribe
    await topic_manager.subscribe_to_topic(client_id, topic)

    # Send initial state
    state_envelope = Envelope.create(
        message_type=message_type,
        topic=topic,
        data=initial_state.model_dump(),
        req_id=message_id,
    )
    await send_to_client_func(client_id, state_envelope.model_dump(), topic=topic)

    # Send ack
    await send_subscribe_ack(client_id, message_id, [topic], send_to_client_func)
