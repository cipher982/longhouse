"""Channel Plugin Interface.

Defines the abstract base class that all channel plugins must implement.
This is the core contract for channel integrations.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from typing import AsyncIterator
from typing import Callable

from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelConfigSchema
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMessageEvent
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelPresence
from zerg.channels.types import ChannelStatus
from zerg.channels.types import ChannelTypingEvent
from zerg.channels.types import MediaAttachment
from zerg.channels.types import MessageDeliveryResult

# Type alias for message handlers
MessageHandler = Callable[[ChannelMessageEvent], None]
TypingHandler = Callable[[ChannelTypingEvent], None]
PresenceHandler = Callable[[ChannelPresence], None]
StatusHandler = Callable[[ChannelStatus], None]


class ChannelPlugin(ABC):
    """Abstract base class for channel plugin implementations.

    Channel plugins provide bi-directional messaging integration with
    external platforms (Telegram, Slack, Discord, etc.).

    Lifecycle:
        1. Plugin is registered with the ChannelRegistry
        2. configure() is called with user's config
        3. start() begins the connection/polling
        4. Message handlers are invoked for incoming messages
        5. stop() cleanly shuts down the connection

    Example implementation:

        class TelegramChannel(ChannelPlugin):
            @property
            def meta(self) -> ChannelMeta:
                return {
                    "id": "telegram",
                    "name": "Telegram",
                    "description": "Telegram Bot API integration"
                }

            async def send_message(self, message: ChannelMessage) -> MessageDeliveryResult:
                # Send via Telegram Bot API
                ...
    """

    # --- Metadata (must be implemented) ---

    @property
    @abstractmethod
    def meta(self) -> ChannelMeta:
        """Return plugin metadata including ID, name, and description."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """Declare what features this channel supports."""
        ...

    @property
    def config_schema(self) -> ChannelConfigSchema | None:
        """Return the configuration schema for this channel.

        Override to define custom configuration fields.
        Returns None if no configuration is needed.
        """
        return None

    # --- Lifecycle ---

    @abstractmethod
    async def configure(self, config: ChannelConfig) -> None:
        """Configure the channel with user settings.

        Called before start() to apply configuration.

        Args:
            config: Channel configuration including credentials

        Raises:
            ValueError: If configuration is invalid
        """
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start the channel connection.

        Begin listening for incoming messages. This should be non-blocking
        and set up any necessary polling, webhooks, or websocket connections.

        Raises:
            ConnectionError: If connection fails
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel connection gracefully.

        Clean up resources and close connections.
        """
        ...

    @property
    @abstractmethod
    def status(self) -> ChannelStatus:
        """Return the current connection status."""
        ...

    # --- Messaging ---

    @abstractmethod
    async def send_message(self, message: ChannelMessage) -> MessageDeliveryResult:
        """Send a message through this channel.

        Args:
            message: The message to send

        Returns:
            MessageDeliveryResult with success status and message ID
        """
        ...

    async def send_media(self, to: str, media: MediaAttachment, caption: str | None = None) -> MessageDeliveryResult:
        """Send media through this channel.

        Default implementation wraps as a message with media attachment.
        Override for platforms with specialized media APIs.

        Args:
            to: Recipient ID
            media: Media attachment to send
            caption: Optional caption

        Returns:
            MessageDeliveryResult with success status
        """
        return await self.send_message(
            ChannelMessage(
                channel_id=self.meta["id"],
                to=to,
                text=caption,
                media=[media],
            )
        )

    async def edit_message(self, message_id: str, text: str, chat_id: str | None = None) -> MessageDeliveryResult:
        """Edit a previously sent message.

        Args:
            message_id: ID of message to edit
            text: New message text
            chat_id: Chat ID (required by some platforms)

        Returns:
            MessageDeliveryResult

        Raises:
            NotImplementedError: If channel doesn't support editing
        """
        raise NotImplementedError(f"{self.meta['id']} does not support message editing")

    async def delete_message(self, message_id: str, chat_id: str | None = None) -> MessageDeliveryResult:
        """Delete a message.

        Args:
            message_id: ID of message to delete
            chat_id: Chat ID (required by some platforms)

        Returns:
            MessageDeliveryResult

        Raises:
            NotImplementedError: If channel doesn't support deletion
        """
        raise NotImplementedError(f"{self.meta['id']} does not support message deletion")

    async def send_reaction(self, message_id: str, reaction: str, chat_id: str | None = None) -> MessageDeliveryResult:
        """React to a message.

        Args:
            message_id: ID of message to react to
            reaction: Emoji or reaction identifier
            chat_id: Chat ID (required by some platforms)

        Returns:
            MessageDeliveryResult

        Raises:
            NotImplementedError: If channel doesn't support reactions
        """
        raise NotImplementedError(f"{self.meta['id']} does not support reactions")

    # --- Presence & Typing ---

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator.

        Args:
            chat_id: Chat to show typing in

        Default implementation is a no-op.
        """
        pass

    async def get_presence(self, user_id: str) -> ChannelPresence | None:
        """Get presence information for a user.

        Args:
            user_id: User ID to check

        Returns:
            ChannelPresence if available, None otherwise
        """
        return None

    # --- Event Handlers ---

    def on_message(self, handler: MessageHandler) -> Callable[[], None]:
        """Register a handler for incoming messages.

        Args:
            handler: Callback for message events

        Returns:
            Unsubscribe function
        """
        if not hasattr(self, "_message_handlers"):
            self._message_handlers: list[MessageHandler] = []
        self._message_handlers.append(handler)
        return lambda: self._message_handlers.remove(handler)

    def on_typing(self, handler: TypingHandler) -> Callable[[], None]:
        """Register a handler for typing events.

        Args:
            handler: Callback for typing events

        Returns:
            Unsubscribe function
        """
        if not hasattr(self, "_typing_handlers"):
            self._typing_handlers: list[TypingHandler] = []
        self._typing_handlers.append(handler)
        return lambda: self._typing_handlers.remove(handler)

    def on_status_change(self, handler: StatusHandler) -> Callable[[], None]:
        """Register a handler for connection status changes.

        Args:
            handler: Callback for status events

        Returns:
            Unsubscribe function
        """
        if not hasattr(self, "_status_handlers"):
            self._status_handlers: list[StatusHandler] = []
        self._status_handlers.append(handler)
        return lambda: self._status_handlers.remove(handler)

    # --- Protected helper methods for subclasses ---

    def _emit_message(self, event: ChannelMessageEvent) -> None:
        """Emit a message event to all registered handlers."""
        for handler in getattr(self, "_message_handlers", []):
            try:
                handler(event)
            except Exception:
                # Don't let handler errors crash the channel
                pass

    def _emit_typing(self, event: ChannelTypingEvent) -> None:
        """Emit a typing event to all registered handlers."""
        for handler in getattr(self, "_typing_handlers", []):
            try:
                handler(event)
            except Exception:
                pass

    def _emit_status(self, status: ChannelStatus) -> None:
        """Emit a status change to all registered handlers."""
        for handler in getattr(self, "_status_handlers", []):
            try:
                handler(status)
            except Exception:
                pass

    # --- Validation ---

    def validate_config(self, config: ChannelConfig) -> list[str]:
        """Validate configuration against schema.

        Args:
            config: Configuration to validate

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        schema = self.config_schema
        if not schema:
            return errors

        for field in schema.get("fields", []):
            key = field["key"]
            required = field.get("required", False)
            creds = config.get("credentials", {})
            settings = config.get("settings", {})

            value = creds.get(key) or settings.get(key)
            if required and not value:
                errors.append(f"Missing required field: {field.get('label', key)}")

        return errors

    # --- Context manager support ---

    @asynccontextmanager
    async def session(self) -> AsyncGenerator["ChannelPlugin", None]:
        """Context manager for channel lifecycle.

        Usage:
            async with channel.session():
                await channel.send_message(...)
        """
        await self.start()
        try:
            yield self
        finally:
            await self.stop()

    # --- Stream interface (optional) ---

    async def message_stream(self) -> AsyncIterator[ChannelMessageEvent]:
        """Async iterator for incoming messages.

        Alternative to callback-based on_message().
        Default implementation raises NotImplementedError.

        Usage:
            async for event in channel.message_stream():
                process(event)

        Raises:
            NotImplementedError: If channel doesn't support streaming
        """
        raise NotImplementedError(f"{self.meta['id']} does not support message streaming")
        # Make this a proper async generator by yielding after the raise
        yield  # type: ignore
