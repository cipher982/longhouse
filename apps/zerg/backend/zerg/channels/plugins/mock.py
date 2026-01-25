"""Mock Channel Plugin.

A mock channel implementation for testing the channel plugin architecture.
Records all sent messages and allows injection of fake incoming messages.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Any
from uuid import uuid4

from zerg.channels.plugin import ChannelPlugin
from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelConfigField
from zerg.channels.types import ChannelConfigSchema
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMessageEvent
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MediaAttachment
from zerg.channels.types import MessageDeliveryResult


class MockChannel(ChannelPlugin):
    """Mock channel for testing.

    Features:
    - Records all sent messages
    - Allows injection of fake incoming messages
    - Simulates delivery delays and failures
    - Configurable behavior via settings

    Usage:
        channel = MockChannel()
        await channel.configure({"enabled": True})
        await channel.start()

        # Send a message
        result = await channel.send_message(ChannelMessage(
            channel_id="mock",
            to="user123",
            text="Hello!"
        ))

        # Check sent messages
        assert len(channel.sent_messages) == 1

        # Inject an incoming message
        channel.inject_message(text="Hi there!", sender_id="user123")
    """

    def __init__(self) -> None:
        """Initialize the mock channel."""
        self._status = ChannelStatus.DISCONNECTED
        self._config: ChannelConfig = {}
        self._sent_messages: deque[ChannelMessage] = deque(maxlen=1000)
        self._message_handlers: list = []
        self._typing_handlers: list = []
        self._status_handlers: list = []

        # Configurable behavior
        self._fail_next_send = False
        self._delay_ms = 0
        self._auto_reply: str | None = None

    @property
    def meta(self) -> ChannelMeta:
        return {
            "id": "mock",
            "name": "Mock Channel",
            "description": "Mock channel for testing",
            "icon": "test-tube",
            "docs_url": "",
            "aliases": ["test", "fake"],
            "order": 999,
        }

    @property
    def capabilities(self) -> ChannelCapabilities:
        return {
            "send_text": True,
            "send_media": True,
            "send_voice": False,
            "send_reactions": True,
            "receive_messages": True,
            "threads": True,
            "replies": True,
            "edit_messages": True,
            "delete_messages": True,
            "polls": False,
            "groups": True,
            "group_management": False,
            "typing_indicator": True,
            "read_receipts": False,
            "presence": False,
            "media_types": ["image/png", "image/jpeg", "application/pdf"],
        }

    @property
    def config_schema(self) -> ChannelConfigSchema:
        return {
            "fields": [
                ChannelConfigField(
                    key="enabled",
                    label="Enable Mock Channel",
                    type="boolean",
                    placeholder="",
                    required=False,
                    default=True,
                ),
                ChannelConfigField(
                    key="delay_ms",
                    label="Simulated Delay (ms)",
                    type="number",
                    placeholder="0",
                    required=False,
                    default=0,
                    help_text="Add artificial delay to message sending",
                ),
                ChannelConfigField(
                    key="auto_reply",
                    label="Auto Reply Text",
                    type="text",
                    placeholder="",
                    required=False,
                    help_text="Automatically reply to messages with this text",
                ),
            ]
        }

    @property
    def status(self) -> ChannelStatus:
        return self._status

    @property
    def sent_messages(self) -> list[ChannelMessage]:
        """Get list of sent messages (most recent first)."""
        return list(self._sent_messages)

    # --- Lifecycle ---

    async def configure(self, config: ChannelConfig) -> None:
        """Configure the mock channel."""
        self._config = config
        settings = config.get("settings", {})
        self._delay_ms = settings.get("delay_ms", 0)
        self._auto_reply = settings.get("auto_reply")

    async def start(self) -> None:
        """Start the mock channel."""
        self._status = ChannelStatus.CONNECTING
        self._emit_status(self._status)

        # Simulate connection delay
        await asyncio.sleep(0.01)

        self._status = ChannelStatus.CONNECTED
        self._emit_status(self._status)

    async def stop(self) -> None:
        """Stop the mock channel."""
        self._status = ChannelStatus.DISCONNECTED
        self._emit_status(self._status)

    # --- Messaging ---

    async def send_message(self, message: ChannelMessage) -> MessageDeliveryResult:
        """Send a message (records it for testing)."""
        # Check for configured failure
        if self._fail_next_send:
            self._fail_next_send = False
            return MessageDeliveryResult(
                success=False,
                error="Simulated failure",
                error_code="MOCK_FAILURE",
            )

        # Simulate delay
        if self._delay_ms > 0:
            await asyncio.sleep(self._delay_ms / 1000)

        # Record the message
        self._sent_messages.appendleft(message)

        message_id = f"mock_{uuid4().hex[:12]}"

        # Auto-reply if configured
        if self._auto_reply and message.get("text"):
            await asyncio.sleep(0.01)
            self.inject_message(
                text=self._auto_reply,
                sender_id=message.get("to", "mock_user"),
                reply_to_id=message_id,
            )

        return MessageDeliveryResult(
            success=True,
            message_id=message_id,
        )

    async def edit_message(self, message_id: str, text: str, chat_id: str | None = None) -> MessageDeliveryResult:
        """Edit a message (no-op in mock)."""
        return MessageDeliveryResult(
            success=True,
            message_id=message_id,
        )

    async def delete_message(self, message_id: str, chat_id: str | None = None) -> MessageDeliveryResult:
        """Delete a message (no-op in mock)."""
        return MessageDeliveryResult(
            success=True,
            message_id=message_id,
        )

    async def send_reaction(self, message_id: str, reaction: str, chat_id: str | None = None) -> MessageDeliveryResult:
        """React to a message (no-op in mock)."""
        return MessageDeliveryResult(
            success=True,
            message_id=message_id,
        )

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        pass

    # --- Test Helpers ---

    def inject_message(
        self,
        *,
        text: str | None = None,
        sender_id: str = "mock_user",
        sender_name: str | None = None,
        chat_id: str | None = None,
        chat_type: str = "dm",
        thread_id: str | None = None,
        reply_to_id: str | None = None,
        media: list[MediaAttachment] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> ChannelMessageEvent:
        """Inject a fake incoming message.

        Creates a message event and dispatches it to handlers.

        Args:
            text: Message text
            sender_id: Sender's ID
            sender_name: Sender's display name
            chat_id: Chat/conversation ID
            chat_type: Type of chat ("dm", "group", "channel")
            thread_id: Thread ID for threaded messages
            reply_to_id: Message ID being replied to
            media: Media attachments
            raw: Raw platform data

        Returns:
            The created message event
        """
        event = ChannelMessageEvent(
            event_id=str(uuid4()),
            channel_id="mock",
            message_id=f"mock_{uuid4().hex[:12]}",
            sender_id=sender_id,
            sender_name=sender_name or sender_id,
            chat_id=chat_id or f"chat_{sender_id}",
            chat_type=chat_type,  # type: ignore
            text=text,
            media=media,
            thread_id=thread_id,
            reply_to_id=reply_to_id,
            timestamp=datetime.utcnow(),
            edited=False,
            is_bot=False,
            raw=raw or {},
        )

        # Dispatch to handlers
        self._emit_message(event)

        return event

    def set_fail_next_send(self, fail: bool = True) -> None:
        """Configure the next send to fail.

        Args:
            fail: Whether to fail the next send
        """
        self._fail_next_send = fail

    def set_delay(self, delay_ms: int) -> None:
        """Set simulated delay for message sending.

        Args:
            delay_ms: Delay in milliseconds
        """
        self._delay_ms = delay_ms

    def set_auto_reply(self, text: str | None) -> None:
        """Set auto-reply text.

        Args:
            text: Text to auto-reply with, or None to disable
        """
        self._auto_reply = text

    def clear_sent_messages(self) -> None:
        """Clear the sent messages buffer."""
        self._sent_messages.clear()

    def get_last_sent(self) -> ChannelMessage | None:
        """Get the most recently sent message."""
        if self._sent_messages:
            return self._sent_messages[0]
        return None


def register_channel() -> ChannelPlugin:
    """Factory function for plugin discovery."""
    return MockChannel()
