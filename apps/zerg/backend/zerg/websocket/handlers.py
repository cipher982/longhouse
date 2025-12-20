"""WebSocket message handlers for topic-based subscriptions.

This module provides handlers for the new topic-based WebSocket system,
supporting subscription to agent and thread events.
"""

import logging
from typing import Any
from typing import Dict
from typing import Optional

from pydantic import BaseModel
from pydantic import ValidationError
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.dependencies.auth import DEV_EMAIL  # noqa: F401  # may be used in future gating
from zerg.generated.ws_messages import AgentEventData

# ---------------------------------------------------------------------------
# Generated message types - single source of truth
# ---------------------------------------------------------------------------
from zerg.generated.ws_messages import Envelope
from zerg.generated.ws_messages import ErrorData
from zerg.generated.ws_messages import MessageType
from zerg.generated.ws_messages import PingData
from zerg.generated.ws_messages import PongData
from zerg.generated.ws_messages import SendMessageData
from zerg.generated.ws_messages import SubscribeData
from zerg.generated.ws_messages import ThreadMessageData
from zerg.generated.ws_messages import UnsubscribeData
from zerg.generated.ws_messages import UserUpdateData
from zerg.websocket.manager import topic_manager

# Import simple subscription helpers
from zerg.websocket.subscription_helpers import send_subscribe_ack
from zerg.websocket.subscription_helpers import send_subscribe_error
from zerg.websocket.subscription_helpers import subscribe_and_send_state

logger = logging.getLogger(__name__)


# SubscribeMessage and UnsubscribeMessage now use generated SubscribeData/UnsubscribeData


# ---------------------------------------------------------------------------
# Simple subscription handlers (no decorators, just functions)
# ---------------------------------------------------------------------------


async def handle_agent_subscription(client_id: str, agent_id: int, message_id: str, db: Session) -> None:
    """Subscribe to agent events."""
    agent = crud.get_agent(db, agent_id)
    if not agent:
        return await send_subscribe_error(
            client_id, message_id, f"Agent {agent_id} not found", [f"agent:{agent_id}"], send_to_client, "NOT_FOUND"
        )

    topic = f"agent:{agent_id}"
    last_run_at = getattr(agent, "last_run_at", None)
    next_run_at = getattr(agent, "next_run_at", None)

    agent_data = AgentEventData(
        id=agent.id,
        status=getattr(agent, "status", None),
        name=getattr(agent, "name", None),
        description=getattr(agent, "system_instructions", None),
        last_run_at=last_run_at.isoformat() if last_run_at else None,
        next_run_at=next_run_at.isoformat() if next_run_at else None,
        last_error=getattr(agent, "last_error", None),
    )

    await subscribe_and_send_state(client_id, topic, message_id, agent_data, "agent_state", send_to_client)


async def handle_user_subscription(client_id: str, user_id: int, message_id: str, db: Session) -> None:
    """Subscribe to user profile updates."""
    topic = f"user:{user_id}"

    # Allow placeholder subscription for user:0
    if user_id <= 0:
        await topic_manager.subscribe_to_topic(client_id, topic)
        await send_subscribe_ack(client_id, message_id, [topic], send_to_client)
        return

    user = crud.get_user(db, user_id)
    if not user:
        return await send_subscribe_error(client_id, message_id, f"User {user_id} not found", [topic], send_to_client, "NOT_FOUND")

    user_data = UserUpdateData(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )

    await subscribe_and_send_state(client_id, topic, message_id, user_data, "user_update", send_to_client)


async def handle_workflow_subscription(client_id: str, execution_id: int, message_id: str, db: Session) -> None:
    """Subscribe to workflow execution events."""
    topic = f"workflow_execution:{execution_id}"
    await topic_manager.subscribe_to_topic(client_id, topic)

    # Try sending current state if execution finished
    try:
        execution = crud.get_workflow_execution(db, execution_id)
        if execution and execution.phase == "finished":
            from zerg.generated.ws_messages import ExecutionFinishedData

            duration_ms = None
            if execution.started_at and execution.finished_at:
                duration_ms = int((execution.finished_at - execution.started_at).total_seconds() * 1000)

            finished_data = ExecutionFinishedData(
                execution_id=execution.id,
                result=execution.result,
                attempt_no=getattr(execution, "attempt_no", 1),
                failure_kind=execution.failure_kind,
                error_message=execution.error_message,
                duration_ms=duration_ms,
            )
            envelope = Envelope.create("execution_finished", topic, finished_data.model_dump(), message_id)
            await send_to_client(client_id, envelope.model_dump(), topic=topic)
    except Exception as e:
        logger.debug(f"No finished state for execution {execution_id}: {e}")

    await send_subscribe_ack(client_id, message_id, [topic], send_to_client)


async def handle_ops_subscription(client_id: str, message_id: str, db: Session) -> None:
    """Subscribe to ops events (admin-only)."""
    topic = "ops:events"
    user_id = topic_manager.client_users.get(client_id)

    if not user_id:
        return await send_subscribe_error(client_id, message_id, "Unauthorized", [topic], send_to_client, "UNAUTHORIZED")

    user = crud.get_user(db, int(user_id))
    if not user or getattr(user, "role", "USER") != "ADMIN":
        return await send_subscribe_error(
            client_id, message_id, "Admin privileges required", [topic], send_to_client, "FORBIDDEN"
        )

    await topic_manager.subscribe_to_topic(client_id, topic)
    await send_subscribe_ack(client_id, message_id, [topic], send_to_client)


async def _subscribe_ops_events(client_id: str, message_id: str, db: Session) -> None:
    """Helper function for tests to subscribe to ops events.

    This is a simplified version that checks admin status and subscribes directly.
    Used by test_ops_events.py.
    """
    topic = "ops:events"
    user_id = topic_manager.client_users.get(client_id)

    if not user_id:
        # For testing: simulate error response
        await send_error(client_id, "Unauthorized", message_id, close_code=1008)
        return

    user = crud.get_user(db, int(user_id))
    if not user or getattr(user, "role", "USER") != "ADMIN":
        # For testing: simulate error response and close connection
        await send_error(client_id, "Admin privileges required", message_id, close_code=1008)
        return

    await topic_manager.subscribe_to_topic(client_id, topic)


# ---------------------------------------------------------------------------
# Core message sending utilities
# ---------------------------------------------------------------------------

async def send_to_client(
    client_id: str,
    message: Dict[str, Any],
    *,
    topic: Optional[str] = None,
) -> bool:
    """Send a message to a client if they are connected.

    Args:
        client_id: The client ID to send to
        message: The message to send

    Returns:
        bool: True if message was sent, False if client not found
    """
    """Low-level helper to push a JSON-serialisable payload to a single client.

    Every outgoing frame **must** follow the unified *Envelope* contract.
    For compatibility, messages that don't already have envelope structure
    are automatically wrapped in envelopes.

    Args:
        client_id: Recipient connection id (uuid4 string)
        message:  Arbitrary JSON-serialisable mapping.  If the mapping lacks
            the mandatory envelope keys (``v``, ``topic``, ``ts``) it will be
            embedded into a new envelope automatically.
        topic:    Optional topic string.  Required when *message* itself does
            not include a topic.  Helpers such as ``_subscribe_agent``
            therefore forward their known topic so the wrapper logic can
            construct a valid envelope.

    Returns:
        True when the frame was queued for sending, False if the *client_id*
        was unknown or the underlying ``send_json`` raised an exception.
    """

    if client_id not in topic_manager.active_connections:
        return False

    # ------------------------------------------------------------------
    # Envelope wrapping
    # ------------------------------------------------------------------

    # Envelope structure is mandatory – wrap any payload that does not yet
    # include the required keys.

    # All outgoing messages must use unified Envelope format
    try:
        # Extract req_id if available from the original message
        req_id = None
        if isinstance(message, dict):
            req_id = message.get("message_id")

        # If message is already an envelope, use it directly
        if isinstance(message, dict) and all(k in message for k in ["v", "type", "topic", "ts", "data"]):
            payload = message
        else:
            # Create envelope for non-envelope messages
            message_type = message.get("type", "unknown") if isinstance(message, dict) else "unknown"
            envelope_topic = topic or "system"
            data = message.get("data", message) if isinstance(message, dict) else message

            envelope = Envelope.create(
                message_type=message_type,
                topic=envelope_topic,
                data=data,
                req_id=req_id,
            )
            payload = envelope.model_dump()

        await topic_manager.active_connections[client_id].send_json(payload)  # type: ignore[arg-type]
        return True
    except Exception as e:  # noqa: BLE001 – log & swallow
        logger.error("Error sending to client %s: %s", client_id, e)
        return False


async def send_error(
    client_id: str,
    error_msg: str,
    message_id: Optional[str] = None,
    *,
    close_code: Optional[int] = None,
) -> None:
    """Send an error message to a client.

    Args:
        client_id: The client ID to send to
        error_msg: The error message
        message_id: Optional message ID to correlate with request
    """
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

    # Optionally close the socket with a specific WebSocket close code.  We
    # perform the close *after* sending the error frame so the client learns
    # the reason before the connection drops.
    if close_code is not None and client_id in topic_manager.active_connections:
        try:
            await topic_manager.active_connections[client_id].close(code=close_code)
        except Exception:  # noqa: BLE001 – ignore errors during close
            pass


async def handle_ping(client_id: str, envelope: Envelope, _: Session) -> None:
    """Handle ping messages to keep connection alive.

    Args:
        client_id: The client ID that sent the ping
        envelope: The ping message envelope
        _: Unused database session
    """
    try:
        ping_data = PingData.model_validate(envelope.data)

        # Build the pong payload using generated types
        pong_data = PongData(
            timestamp=ping_data.timestamp,
        )

        # Recompute envelope flag on **every** ping so tests that mutate the
        # environment mid-process pick up the change from :pyfunc:`get_settings`.
        # Always wrap in the unified Envelope format.

        # Wrap in protocol-level envelope so the frontend can rely on the
        # unified structure.  We treat all ping/pong traffic as a
        # *system* message which is consistent with the heartbeat logic.
        response_envelope = Envelope.create(
            message_type="pong",
            topic="system",
            data=pong_data.model_dump(),
            req_id=envelope.req_id,
        )

        await send_to_client(client_id, response_envelope.model_dump())

    except Exception as e:
        logger.error("Error handling ping: %s", e)
        await send_error(client_id, "Failed to process ping")


# ---------------------------------------------------------------------------
# Heart-beat response (client → pong)
# ---------------------------------------------------------------------------


async def handle_pong(client_id: str, envelope: Envelope, _: Session) -> None:  # noqa: D401
    """Handle *pong* frames sent by clients.

    Simply updates the TopicConnectionManager watchdog so the connection is
    considered alive. No response is sent back to the client.
    """

    try:
        # Validate schema (already done centrally) then record pong.
        topic_manager.record_pong(client_id)
    except Exception as exc:
        logger.debug("Failed to record pong from %s: %s", client_id, exc)


async def handle_subscribe(client_id: str, envelope: Envelope, db: Session) -> None:
    """Handle topic subscription requests using modern router.

    Args:
        client_id: The client ID that sent the message
        envelope: The subscription message envelope
        db: Database session
    """
    try:
        subscribe_data = SubscribeData.model_validate(envelope.data)
        message_id = subscribe_data.message_id or envelope.req_id or "unknown"

        for topic in subscribe_data.topics:
            try:
                prefix, topic_id = topic.split(":", 1)

                # Simple routing - no fancy abstraction needed
                if prefix == "agent":
                    await handle_agent_subscription(client_id, int(topic_id), message_id, db)
                elif prefix == "user":
                    await handle_user_subscription(client_id, int(topic_id), message_id, db)
                elif prefix == "workflow_execution":
                    await handle_workflow_subscription(client_id, int(topic_id), message_id, db)
                elif prefix == "ops" and topic_id == "events":
                    await handle_ops_subscription(client_id, message_id, db)
                elif prefix == "thread":
                    await send_subscribe_error(
                        client_id,
                        message_id,
                        "Thread subscriptions deprecated. Use user:{user_id}",
                        [topic],
                        send_to_client,
                        "DEPRECATED",
                    )
                else:
                    await send_subscribe_error(client_id, message_id, f"Unknown topic type: {prefix}", [topic], send_to_client, "UNKNOWN")

            except (ValueError, IndexError):
                await send_subscribe_error(client_id, message_id, f"Invalid topic format: {topic}", [topic], send_to_client, "INVALID_FORMAT")

    except ValidationError as e:
        logger.error(f"Invalid subscription data: {e}")
        await send_error(client_id, f"Invalid subscription format: {str(e)}", envelope.req_id)
    except Exception as e:
        logger.error(f"Error handling subscription: {str(e)}")
        await send_error(client_id, "Failed to process subscription", envelope.req_id)


async def handle_unsubscribe(client_id: str, message: Dict[str, Any], _: Session) -> None:
    """Handle topic unsubscription requests.

    Args:
        client_id: The client ID that sent the message
        message: The unsubscription message
        _: Unused database session
    """
    try:
        message_id = message.get("message_id", "")
        for topic in message.get("topics", []):
            await topic_manager.unsubscribe_from_topic(client_id, topic)

        # Send confirmation message back to client
        await send_to_client(
            client_id,
            {
                "type": "unsubscribe_success",
                "message_id": message_id,
                "topics": message.get("topics", []),
            },
        )
    except Exception as e:
        logger.error(f"Error handling unsubscribe: {str(e)}")
        await send_error(client_id, "Failed to process unsubscribe", message.get("message_id", ""))


# Message handler dispatcher
MESSAGE_HANDLERS = {
    "ping": handle_ping,
    "pong": handle_pong,
    "subscribe": handle_subscribe,
    "unsubscribe": handle_unsubscribe,
    # Thread‑specific handlers used by higher‑level chat API
    "subscribe_thread": None,  # populated below
    "send_message": None,  # populated below
}

# ---------------------------------------------------------------------------
# Runtime inbound payload validation (Phase 2 groundwork)
# ---------------------------------------------------------------------------

# Mapping of *client → server* message types to their strict Pydantic models.
# We validate the incoming JSON before it reaches individual handlers so that
# malformed payloads are rejected consistently in one place.

_INBOUND_SCHEMA_MAP: Dict[str, type[BaseModel]] = {
    "ping": PingData,
    "pong": PongData,
    "subscribe": SubscribeData,
    "unsubscribe": UnsubscribeData,
    "send_message": SendMessageData,
    # Note: All messages now validated as envelope + payload data
}


# ---------------------------------------------------------------------------
# Dedicated helpers for chat‑centric message types introduced in
# the legacy WebSocket protocol. They wrap the existing topic
# subscription mechanism so the rest of the system (topic_manager &
# event_bus) continues to operate unchanged.


async def handle_subscribe_thread(client_id: str, message: Dict[str, Any], db: Session) -> None:  # noqa: D401
    """Handle a *subscribe_thread* request.

    DEPRECATED: Thread subscriptions removed. All streaming now goes to user:{user_id} topic.
    """
    message_id = message.get("message_id", "")
    await send_error(
        client_id,
        "subscribe_thread is deprecated. All streaming is automatically delivered to user:{user_id}",
        message_id,
    )


async def handle_send_message(client_id: str, message: Dict[str, Any], db: Session) -> None:  # noqa: D401
    """Persist a new message to a thread and broadcast it."""

    try:
        # Extract and validate data from message dict
        thread_id = message.get("thread_id")
        content = message.get("content")
        message_id = message.get("message_id", "")

        if thread_id is None or content is None:
            await send_error(client_id, "Missing thread_id or content in send_message", message_id)
            return

        # Validate thread exists and get agent owner
        thread = crud.get_thread(db, thread_id)
        if not thread:
            await send_error(client_id, f"Thread {thread_id} not found", message_id)
            return

        # Get agent to find owner_id
        agent = crud.get_agent(db, agent_id=thread.agent_id)
        if not agent:
            await send_error(client_id, f"Agent for thread {thread_id} not found", message_id)
            return

        # Persist the message
        db_msg = crud.create_thread_message(
            db,
            thread_id=thread_id,
            role="user",
            content=content,
            processed=False,
        )

        msg_dict = {
            "id": db_msg.id,
            "thread_id": db_msg.thread_id,
            "role": db_msg.role,
            "content": db_msg.content,
            "timestamp": db_msg.sent_at.isoformat() if db_msg.sent_at else None,
            "processed": db_msg.processed,
        }

        # Create structured ThreadMessageData
        thread_msg_data = ThreadMessageData(
            thread_id=thread_id,
            message=msg_dict,
        )

        # Broadcast to user-scoped topic (all streaming goes to user:{user_id})
        topic = f"user:{agent.owner_id}"
        envelope = Envelope.create(
            message_type="thread_message",
            topic=topic,
            data=thread_msg_data.model_dump(),
            req_id=message_id,
        )

        await topic_manager.broadcast_to_topic(topic, envelope.model_dump())

        # We intentionally do **not** publish a secondary
        # ``THREAD_MESSAGE_CREATED`` event here. The freshly‑created
        # ``thread_message`` broadcast above already informs every subscriber
        # of the new content. Emitting an additional event would be redundant
        # and lead to duplicate WebSocket payloads (see chat flow tests).

    except Exception as e:
        logger.error(f"Error in handle_send_message: {str(e)}")
        await send_error(client_id, "Failed to send message", message.get("message_id"))


# Register the chat-specific handlers in the dispatcher
MESSAGE_HANDLERS["subscribe_thread"] = handle_subscribe_thread
MESSAGE_HANDLERS["send_message"] = handle_send_message


async def dispatch_message(client_id: str, message: Dict[str, Any], db: Session) -> None:
    """Dispatch a message to the appropriate handler.

    Args:
        client_id: The client ID that sent the message
        message: The message to dispatch (raw dict or envelope)
        db: Database session
    """
    try:
        # Handle both envelope and legacy format messages
        if "type" in message and "data" in message and "topic" in message:
            # New envelope format
            envelope = Envelope.model_validate(message)
            message_type = envelope.type
            message_data = envelope.data
        else:
            # Legacy format - convert to envelope-like structure
            message_type = message.get("type")
            envelope = None
            message_data = message

        # ------------------------------------------------------------------
        # 1) Fast-fail on completely unknown "type" field.
        # ------------------------------------------------------------------
        if message_type not in MESSAGE_HANDLERS:
            await send_error(client_id, f"Unknown message type: {message_type}")
            return

        # ------------------------------------------------------------------
        # 2) Schema validation using Pydantic models defined in
        #    ``_INBOUND_SCHEMA_MAP``.  We do **not** trust handlers to repeat
        #    validation – doing it centrally prevents duplicate effort and
        #    guarantees identical error semantics across all message types.
        # ------------------------------------------------------------------

        model_cls = _INBOUND_SCHEMA_MAP.get(message_type)
        if model_cls is not None:
            try:
                # Validate the data portion, not the entire envelope
                model_cls.model_validate(message_data)
            except ValidationError as exc:
                logger.debug("Schema validation failed for %s: %s", message_type, exc)
                await send_error(
                    client_id,
                    "INVALID_PAYLOAD",
                    envelope.req_id if envelope else message.get("message_id"),
                    close_code=1002,
                )
                # Draft spec says we should close with 1002 on protocol error;
                return

        # Forward to handler - pass envelope for envelope-aware handlers, message for legacy handlers
        handler = MESSAGE_HANDLERS[message_type]
        if message_type in ["ping", "pong", "subscribe"]:
            # These handlers expect Envelope objects
            if envelope is None:
                # Create envelope for legacy messages
                envelope = Envelope.create(
                    message_type=message_type, topic="system", data=message_data, req_id=message.get("message_id")
                )
            await handler(client_id, envelope, db)
        else:
            # Legacy handlers expect dict messages
            await handler(client_id, message, db)

    except Exception as e:
        logger.error(f"Error dispatching message: {str(e)}")
        await send_error(client_id, "Failed to process message")


__all__ = ["dispatch_message"]
