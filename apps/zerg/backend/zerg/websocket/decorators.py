"""WebSocket subscription handler decorators and context utilities.

This module provides a clean decorator-based framework for subscription handlers
that automatically handles:
- Sending initial state
- Sending subscription acknowledgments
- Sending error responses
- Consistent error handling and logging
"""

import logging
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.generated.ws_messages import Envelope, SubscribeAckData, SubscribeErrorData
from zerg.websocket.manager import topic_manager

logger = logging.getLogger(__name__)

# Registry for subscription handlers by topic prefix
SUBSCRIPTION_HANDLERS: Dict[str, Callable] = {}


class SubscriptionContext:
    """Context object passed to subscription handlers.

    Provides a clean API for common subscription operations:
    - Accessing request parameters (client_id, message_id, db, parsed topic params)
    - Sending successful responses (initial state + ack)
    - Sending error responses
    """

    def __init__(
        self,
        client_id: str,
        message_id: str,
        db: Session,
        topic: str,
        params: Dict[str, Any]
    ):
        """Initialize subscription context.

        Args:
            client_id: WebSocket client ID
            message_id: Message ID for correlation
            db: Database session
            topic: Full topic string (e.g., "agent:123")
            params: Extracted parameters from topic (e.g., {"agent_id": 123})
        """
        self.client_id = client_id
        self.message_id = message_id
        self.db = db
        self.topic = topic
        self.params = params

    async def success(
        self,
        initial_state: BaseModel,
        message_type: Optional[str] = None,
    ) -> None:
        """Send successful subscription response.

        Automatically:
        1. Subscribes client to the topic
        2. Sends initial state to client
        3. Sends subscription acknowledgment

        Args:
            initial_state: Pydantic model containing initial entity state
            message_type: Override message type (defaults to "{prefix}_state")
        """
        # Subscribe to topic
        await topic_manager.subscribe_to_topic(self.client_id, self.topic)

        # Determine message type from topic if not provided
        if message_type is None:
            topic_prefix = self.topic.split(":")[0]
            message_type = f"{topic_prefix}_state"

        # Send initial state
        state_envelope = Envelope.create(
            message_type=message_type,
            topic=self.topic,
            data=initial_state.model_dump(),
            req_id=self.message_id
        )

        # Import here to avoid circular dependency
        from zerg.websocket.handlers import send_to_client

        await send_to_client(
            self.client_id,
            state_envelope.model_dump(),
            topic=self.topic
        )

        logger.info(
            f"Sent initial {message_type} for {self.topic} to client {self.client_id}"
        )

        # Send subscription acknowledgment
        ack_data = SubscribeAckData(
            message_id=self.message_id,
            topics=[self.topic]
        )
        ack_envelope = Envelope.create(
            message_type="subscribe_ack",
            topic="system",
            data=ack_data.model_dump(),
            req_id=self.message_id
        )

        await send_to_client(
            self.client_id,
            ack_envelope.model_dump(),
            topic="system"
        )

        logger.debug(f"Sent subscribe_ack for {self.topic} to client {self.client_id}")

    async def error(
        self,
        error_msg: str,
        error_code: Optional[str] = None,
        topics: Optional[List[str]] = None
    ) -> None:
        """Send subscription error response.

        Args:
            error_msg: Human-readable error message
            error_code: Machine-readable error code (e.g., "NOT_FOUND", "FORBIDDEN")
            topics: List of topics that failed (defaults to current topic)
        """
        error_data = SubscribeErrorData(
            message_id=self.message_id,
            topics=topics or [self.topic],
            error=error_msg,
            error_code=error_code
        )

        error_envelope = Envelope.create(
            message_type="subscribe_error",
            topic="system",
            data=error_data.model_dump(),
            req_id=self.message_id
        )

        # Import here to avoid circular dependency
        from zerg.websocket.handlers import send_to_client

        await send_to_client(
            self.client_id,
            error_envelope.model_dump(),
            topic="system"
        )

        logger.warning(
            f"Subscription error for {self.topic}: {error_msg} (code: {error_code})"
        )


def subscription_handler(topic_prefix: str):
    """Decorator for subscription handlers.

    Usage:
        @subscription_handler("agent")
        async def handle_agent_subscription(ctx: SubscriptionContext):
            agent_id = ctx.params["agent_id"]
            agent = crud.get_agent(ctx.db, agent_id)

            if not agent:
                return await ctx.error(f"Agent {agent_id} not found", "NOT_FOUND")

            return await ctx.success(
                initial_state=AgentEventData.from_orm(agent)
            )

    Args:
        topic_prefix: Topic prefix to handle (e.g., "agent", "user", "thread")
    """

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(
            client_id: str,
            topic: str,
            params: Dict[str, Any],
            message_id: str,
            db: Session
        ):
            """Wrapper that creates context and handles exceptions."""
            ctx = SubscriptionContext(
                client_id=client_id,
                message_id=message_id,
                db=db,
                topic=topic,
                params=params
            )

            try:
                return await func(ctx)
            except Exception as e:
                logger.exception(f"Handler {func.__name__} failed for topic {topic}")
                await ctx.error(
                    error_msg=f"Internal error: {str(e)}",
                    error_code="INTERNAL_ERROR"
                )

        # Register handler in global registry
        SUBSCRIPTION_HANDLERS[topic_prefix] = wrapper
        return wrapper

    return decorator


def parse_topic_params(topic: str) -> Dict[str, Any]:
    """Parse parameters from topic string.

    Examples:
        "agent:123" -> {"agent_id": 123}
        "thread:456" -> {"thread_id": 456}
        "user:789" -> {"user_id": 789}
        "workflow_execution:999" -> {"execution_id": 999}
        "ops:events" -> {}

    Args:
        topic: Topic string to parse

    Returns:
        Dictionary of parsed parameters
    """
    parts = topic.split(":", 1)
    if len(parts) != 2:
        return {}

    prefix, value = parts

    # Map topic prefixes to parameter names
    param_map = {
        "agent": "agent_id",
        "thread": "thread_id",
        "user": "user_id",
        "workflow_execution": "execution_id",
    }

    param_name = param_map.get(prefix)
    if param_name and value.isdigit():
        return {param_name: int(value)}

    return {}


async def route_subscription(
    client_id: str,
    topic: str,
    message_id: str,
    db: Session
) -> bool:
    """Route subscription request to appropriate handler.

    Args:
        client_id: WebSocket client ID
        topic: Topic to subscribe to
        message_id: Message ID for correlation
        db: Database session

    Returns:
        True if handler was found and executed, False otherwise
    """
    # Extract topic prefix
    topic_prefix = topic.split(":")[0]

    # Find handler
    handler = SUBSCRIPTION_HANDLERS.get(topic_prefix)
    if not handler:
        logger.warning(f"No subscription handler for topic prefix: {topic_prefix}")
        return False

    # Parse topic parameters
    params = parse_topic_params(topic)

    # Execute handler
    await handler(
        client_id=client_id,
        topic=topic,
        params=params,
        message_id=message_id,
        db=db
    )

    return True
