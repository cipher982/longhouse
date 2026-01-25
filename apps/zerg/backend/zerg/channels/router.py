"""Channel Router.

Routes messages between the Zerg agent system and channel plugins.
Handles message dispatch, event routing, and conversation state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Any
from typing import Callable
from uuid import uuid4

from zerg.channels.plugin import ChannelPlugin
from zerg.channels.registry import ChannelRegistry
from zerg.channels.registry import get_registry
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMessageEvent
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MessageDeliveryResult

logger = logging.getLogger(__name__)


@dataclass
class RoutedMessage:
    """A message that has been routed through the system."""

    id: str
    channel_id: str
    direction: str  # "inbound" or "outbound"
    message: ChannelMessage | ChannelMessageEvent
    timestamp: datetime
    delivery_result: MessageDeliveryResult | None = None


@dataclass
class ConversationContext:
    """Conversation context for message routing."""

    conversation_id: str
    channel_id: str
    chat_id: str
    thread_id: str | None = None
    last_message_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Type aliases
InboundHandler = Callable[[ChannelMessageEvent, ConversationContext], None]
OutboundFilter = Callable[[ChannelMessage], ChannelMessage | None]


class ChannelRouter:
    """Routes messages between Zerg agents and channel plugins.

    The router:
    - Receives incoming messages from channels and dispatches to agents
    - Sends outgoing messages from agents to appropriate channels
    - Maintains conversation context for multi-turn interactions
    - Applies message filters and transformations

    Example:
        router = ChannelRouter()
        router.on_inbound(handle_user_message)

        # Send a message
        await router.send("telegram", to="@user", text="Hello!")
    """

    def __init__(
        self,
        registry: ChannelRegistry | None = None,
    ) -> None:
        """Initialize the router.

        Args:
            registry: Channel registry to use (default: global registry)
        """
        self._registry = registry or get_registry()
        self._inbound_handlers: list[InboundHandler] = []
        self._outbound_filters: list[OutboundFilter] = []
        self._conversations: dict[str, ConversationContext] = {}
        self._message_history: list[RoutedMessage] = []
        self._unsubscribe_fns: list[Callable[[], None]] = []
        self._running = False

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the router and subscribe to all channels."""
        if self._running:
            return

        self._running = True

        # Subscribe to messages from all channels
        for channel in self._registry.list():
            unsub = channel.on_message(lambda event, ch=channel: self._handle_inbound(event, ch))
            self._unsubscribe_fns.append(unsub)

            # Subscribe to status changes
            unsub_status = channel.on_status_change(lambda status, ch=channel: self._handle_status_change(status, ch))
            self._unsubscribe_fns.append(unsub_status)

        logger.info(f"Router started, listening to {len(self._registry.list())} channels")

    async def stop(self) -> None:
        """Stop the router and unsubscribe from all channels."""
        self._running = False

        # Unsubscribe from all channels
        for unsub in self._unsubscribe_fns:
            try:
                unsub()
            except Exception:
                pass

        self._unsubscribe_fns.clear()
        logger.info("Router stopped")

    # --- Message Handling ---

    def _handle_inbound(self, event: ChannelMessageEvent, channel: ChannelPlugin) -> None:
        """Handle an incoming message from a channel."""
        try:
            # Get or create conversation context
            conversation = self._get_or_create_conversation(event, channel)

            # Record the message
            routed = RoutedMessage(
                id=str(uuid4()),
                channel_id=event.get("channel_id", channel.meta["id"]),
                direction="inbound",
                message=event,
                timestamp=datetime.utcnow(),
            )
            self._message_history.append(routed)

            # Dispatch to handlers
            for handler in self._inbound_handlers:
                try:
                    handler(event, conversation)
                except Exception as e:
                    logger.error(f"Inbound handler error: {e}")

        except Exception as e:
            logger.error(f"Error handling inbound message: {e}")

    def _handle_status_change(self, status: ChannelStatus, channel: ChannelPlugin) -> None:
        """Handle a channel status change."""
        logger.info(f"Channel {channel.meta['id']} status: {status.value}")

    def _get_or_create_conversation(
        self,
        event: ChannelMessageEvent,
        channel: ChannelPlugin,
    ) -> ConversationContext:
        """Get or create conversation context for a message."""
        channel_id = event.get("channel_id", channel.meta["id"])
        chat_id = event.get("chat_id", "")
        thread_id = event.get("thread_id")

        # Build conversation key
        conv_key = f"{channel_id}:{chat_id}"
        if thread_id:
            conv_key += f":{thread_id}"

        if conv_key not in self._conversations:
            self._conversations[conv_key] = ConversationContext(
                conversation_id=conv_key,
                channel_id=channel_id,
                chat_id=chat_id,
                thread_id=thread_id,
            )

        conversation = self._conversations[conv_key]
        conversation.last_message_at = datetime.utcnow()
        return conversation

    # --- Sending Messages ---

    async def send(
        self,
        channel_id: str,
        *,
        to: str,
        text: str | None = None,
        **kwargs: Any,
    ) -> MessageDeliveryResult:
        """Send a message through a channel.

        Args:
            channel_id: Target channel ID
            to: Recipient ID
            text: Message text
            **kwargs: Additional message options

        Returns:
            MessageDeliveryResult

        Raises:
            ValueError: If channel not found
        """
        channel = self._registry.get(channel_id)
        if not channel:
            return MessageDeliveryResult(
                success=False,
                error=f"Channel not found: {channel_id}",
                error_code="CHANNEL_NOT_FOUND",
            )

        message = ChannelMessage(
            channel_id=channel_id,
            to=to,
            text=text,
            **kwargs,
        )

        return await self.send_message(message)

    async def send_message(self, message: ChannelMessage) -> MessageDeliveryResult:
        """Send a message object through its channel.

        Args:
            message: The message to send

        Returns:
            MessageDeliveryResult
        """
        channel_id = message.get("channel_id", "")
        channel = self._registry.get(channel_id)

        if not channel:
            return MessageDeliveryResult(
                success=False,
                error=f"Channel not found: {channel_id}",
                error_code="CHANNEL_NOT_FOUND",
            )

        # Apply outbound filters
        filtered_message: ChannelMessage | None = message
        for filter_fn in self._outbound_filters:
            try:
                filtered_message = filter_fn(filtered_message)  # type: ignore
                if filtered_message is None:
                    return MessageDeliveryResult(
                        success=False,
                        error="Message blocked by filter",
                        error_code="FILTERED",
                    )
            except Exception as e:
                logger.error(f"Outbound filter error: {e}")

        # Send the message
        try:
            result = await channel.send_message(filtered_message)  # type: ignore

            # Record the message
            routed = RoutedMessage(
                id=str(uuid4()),
                channel_id=channel_id,
                direction="outbound",
                message=message,
                timestamp=datetime.utcnow(),
                delivery_result=result,
            )
            self._message_history.append(routed)

            return result

        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return MessageDeliveryResult(
                success=False,
                error=str(e),
                error_code="SEND_ERROR",
            )

    async def broadcast(
        self,
        message: ChannelMessage,
        channels: list[str] | None = None,
    ) -> dict[str, MessageDeliveryResult]:
        """Broadcast a message to multiple channels.

        Args:
            message: Base message to broadcast
            channels: List of channel IDs (default: all enabled channels)

        Returns:
            Dict mapping channel_id to delivery result
        """
        target_channels = channels or self._registry.list_ids()
        results: dict[str, MessageDeliveryResult] = {}

        tasks = []
        for channel_id in target_channels:
            channel_message = ChannelMessage(**message)  # type: ignore
            channel_message["channel_id"] = channel_id
            tasks.append((channel_id, self.send_message(channel_message)))

        # Execute in parallel
        for channel_id, task in tasks:
            try:
                results[channel_id] = await task
            except Exception as e:
                results[channel_id] = MessageDeliveryResult(
                    success=False,
                    error=str(e),
                    error_code="BROADCAST_ERROR",
                )

        return results

    # --- Event Handlers ---

    def on_inbound(self, handler: InboundHandler) -> Callable[[], None]:
        """Register a handler for incoming messages.

        Args:
            handler: Callback receiving (event, conversation_context)

        Returns:
            Unsubscribe function
        """
        self._inbound_handlers.append(handler)
        return lambda: self._inbound_handlers.remove(handler)

    def add_outbound_filter(self, filter_fn: OutboundFilter) -> Callable[[], None]:
        """Add a filter for outbound messages.

        Filters can modify or block outgoing messages.
        Return None from the filter to block the message.

        Args:
            filter_fn: Filter function (message) -> modified_message | None

        Returns:
            Function to remove the filter
        """
        self._outbound_filters.append(filter_fn)
        return lambda: self._outbound_filters.remove(filter_fn)

    # --- Conversation Management ---

    def get_conversation(self, conversation_id: str) -> ConversationContext | None:
        """Get a conversation context by ID.

        Args:
            conversation_id: Conversation ID

        Returns:
            ConversationContext if found
        """
        return self._conversations.get(conversation_id)

    def list_conversations(
        self,
        channel_id: str | None = None,
    ) -> list[ConversationContext]:
        """List active conversations.

        Args:
            channel_id: Filter by channel (optional)

        Returns:
            List of conversation contexts
        """
        conversations = list(self._conversations.values())
        if channel_id:
            conversations = [c for c in conversations if c.channel_id == channel_id]
        return conversations

    def clear_conversation(self, conversation_id: str) -> bool:
        """Clear a conversation context.

        Args:
            conversation_id: Conversation to clear

        Returns:
            True if conversation was cleared
        """
        return self._conversations.pop(conversation_id, None) is not None

    # --- History ---

    def get_message_history(
        self,
        channel_id: str | None = None,
        direction: str | None = None,
        limit: int = 100,
    ) -> list[RoutedMessage]:
        """Get message history.

        Args:
            channel_id: Filter by channel
            direction: Filter by direction ("inbound" or "outbound")
            limit: Maximum messages to return

        Returns:
            List of routed messages (most recent first)
        """
        history = self._message_history

        if channel_id:
            history = [m for m in history if m.channel_id == channel_id]
        if direction:
            history = [m for m in history if m.direction == direction]

        return list(reversed(history[-limit:]))
