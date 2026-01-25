"""Tests for the Channel SDK utilities."""

from __future__ import annotations

from datetime import datetime

import pytest

from zerg.channels.sdk import BaseChannel
from zerg.channels.sdk import PollingChannel
from zerg.channels.sdk import WebhookChannel
from zerg.channels.sdk import chunk_text
from zerg.channels.sdk import create_message_event
from zerg.channels.types import ChannelCapabilities
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelMeta
from zerg.channels.types import ChannelStatus
from zerg.channels.types import MessageDeliveryResult


class TestCreateMessageEvent:
    """Tests for create_message_event helper."""

    def test_minimal_event(self):
        """Test creating minimal message event."""
        event = create_message_event(
            channel_id="telegram",
            message_id="msg_123",
            text="Hello!",
        )

        assert event["channel_id"] == "telegram"
        assert event["message_id"] == "msg_123"
        assert event["text"] == "Hello!"
        assert event["event_id"] is not None

    def test_full_event(self):
        """Test creating full message event."""
        ts = datetime.utcnow()
        event = create_message_event(
            channel_id="slack",
            message_id="msg_456",
            text="Thread message",
            sender_id="U12345",
            chat_id="C67890",
            sender_name="John Doe",
            chat_type="group",
            thread_id="thread_123",
            timestamp=ts,
        )

        assert event["sender_id"] == "U12345"
        assert event["chat_id"] == "C67890"
        assert event["sender_name"] == "John Doe"
        assert event["chat_type"] == "group"
        assert event["thread_id"] == "thread_123"
        assert event["timestamp"] == ts


class TestChunkText:
    """Tests for chunk_text helper."""

    def test_short_text_no_chunking(self):
        """Test that short text isn't chunked."""
        text = "This is a short message."
        chunks = chunk_text(text, max_length=100)

        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_chunked(self):
        """Test that long text is chunked."""
        text = "A" * 100
        chunks = chunk_text(text, max_length=30)

        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 30

    def test_chunk_on_paragraph(self):
        """Test chunking on paragraph boundary."""
        text = "First paragraph.\n\nSecond paragraph that is longer."
        chunks = chunk_text(text, max_length=25)

        assert len(chunks) >= 2
        assert chunks[0] == "First paragraph."

    def test_chunk_on_sentence(self):
        """Test chunking on sentence boundary."""
        text = "First sentence. Second sentence. Third sentence."
        chunks = chunk_text(text, max_length=35)

        assert chunks[0].endswith(".")

    def test_chunk_preserves_all_text(self):
        """Test that all text is preserved when chunking."""
        text = "This is a test message that should be split into multiple chunks."
        chunks = chunk_text(text, max_length=20)

        # Reconstruct and check (allowing for whitespace differences)
        reconstructed = " ".join(chunks)
        # Check that all words are present
        for word in text.split():
            assert word in reconstructed


class ConcreteBaseChannel(BaseChannel):
    """Concrete implementation for testing BaseChannel."""

    def __init__(self):
        super().__init__()
        self._connected = False
        self._sent_messages = []

    @property
    def meta(self) -> ChannelMeta:
        return {
            "id": "test_base",
            "name": "Test Base Channel",
            "description": "Test implementation",
        }

    @property
    def capabilities(self) -> ChannelCapabilities:
        return {
            "send_text": True,
            "receive_messages": True,
        }

    async def _do_connect(self) -> None:
        self._connected = True

    async def _do_disconnect(self) -> None:
        self._connected = False

    async def _do_send(self, message: ChannelMessage) -> MessageDeliveryResult:
        self._sent_messages.append(message)
        return MessageDeliveryResult(success=True, message_id="test_msg_123")


class TestBaseChannel:
    """Tests for BaseChannel."""

    @pytest.mark.asyncio
    async def test_configure(self):
        """Test channel configuration."""
        channel = ConcreteBaseChannel()
        config: ChannelConfig = {
            "channel_id": "test_base",
            "enabled": True,
        }
        await channel.configure(config)
        assert channel._config == config

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Test start and stop."""
        channel = ConcreteBaseChannel()
        await channel.configure({})

        await channel.start()
        assert channel.status == ChannelStatus.CONNECTED
        assert channel._connected is True

        await channel.stop()
        assert channel.status == ChannelStatus.DISCONNECTED
        assert channel._connected is False

    @pytest.mark.asyncio
    async def test_send_message(self):
        """Test sending message."""
        channel = ConcreteBaseChannel()
        await channel.configure({})
        await channel.start()

        result = await channel.send_message(ChannelMessage(channel_id="test_base", to="user1", text="Hello"))

        assert result["success"] is True
        assert len(channel._sent_messages) == 1

        await channel.stop()

    @pytest.mark.asyncio
    async def test_send_when_disconnected_fails(self):
        """Test that send fails when disconnected."""
        channel = ConcreteBaseChannel()
        await channel.configure({})
        # Don't start

        result = await channel.send_message(ChannelMessage(channel_id="test_base", to="user1", text="Hello"))

        assert result["success"] is False
        assert result["error_code"] == "NOT_CONNECTED"

    @pytest.mark.asyncio
    async def test_status_change_emitted(self):
        """Test that status changes are emitted."""
        channel = ConcreteBaseChannel()

        statuses = []
        channel.on_status_change(lambda s: statuses.append(s))

        await channel.configure({})
        await channel.start()

        assert ChannelStatus.CONNECTING in statuses
        assert ChannelStatus.CONNECTED in statuses

        await channel.stop()


class FailingChannel(BaseChannel):
    """Channel that fails on send for testing retry logic."""

    def __init__(self):
        super().__init__()
        self.attempt_count = 0
        self.fail_count = 2  # Fail first N attempts

    @property
    def meta(self) -> ChannelMeta:
        return {"id": "failing", "name": "Failing Channel", "description": ""}

    @property
    def capabilities(self) -> ChannelCapabilities:
        return {"send_text": True}

    async def _do_connect(self) -> None:
        pass

    async def _do_disconnect(self) -> None:
        pass

    async def _do_send(self, message: ChannelMessage) -> MessageDeliveryResult:
        self.attempt_count += 1
        if self.attempt_count <= self.fail_count:
            raise ConnectionError("Simulated failure")
        return MessageDeliveryResult(success=True, message_id="success")


class TestBaseChannelRetry:
    """Tests for BaseChannel retry logic."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Test that send retries on failure."""
        channel = FailingChannel()
        channel._max_retries = 3
        channel._retry_delay_base = 0.01  # Speed up test

        await channel.configure({})
        await channel.start()

        result = await channel.send_message(ChannelMessage(channel_id="failing", to="user1", text="Test"))

        # Should succeed after retries
        assert result["success"] is True
        assert channel.attempt_count == 3  # 2 failures + 1 success

        await channel.stop()

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """Test behavior when max retries exceeded."""
        channel = FailingChannel()
        channel.fail_count = 10  # Always fail
        channel._max_retries = 3
        channel._retry_delay_base = 0.01

        await channel.configure({})
        await channel.start()

        result = await channel.send_message(ChannelMessage(channel_id="failing", to="user1", text="Test"))

        assert result["success"] is False
        assert result["error_code"] == "MAX_RETRIES"
        assert channel.attempt_count == 3

        await channel.stop()


class ConcretePollingChannel(PollingChannel):
    """Concrete implementation for testing PollingChannel."""

    def __init__(self):
        super().__init__(poll_interval=0.05)  # Fast polling for tests
        self.poll_count = 0
        self.messages_to_return = []

    @property
    def meta(self) -> ChannelMeta:
        return {"id": "polling", "name": "Polling Channel", "description": ""}

    @property
    def capabilities(self) -> ChannelCapabilities:
        return {"receive_messages": True, "send_text": True}

    async def _poll_messages(self):
        self.poll_count += 1
        msgs = self.messages_to_return.copy()
        self.messages_to_return.clear()
        return msgs

    async def _do_send(self, message: ChannelMessage) -> MessageDeliveryResult:
        return MessageDeliveryResult(success=True, message_id="msg_123")


class TestPollingChannel:
    """Tests for PollingChannel."""

    @pytest.mark.asyncio
    async def test_polling_starts(self):
        """Test that polling starts on connect."""
        channel = ConcretePollingChannel()
        await channel.configure({})
        await channel.start()

        # Wait for a few polls
        import asyncio

        await asyncio.sleep(0.15)

        assert channel.poll_count >= 2

        await channel.stop()

    @pytest.mark.asyncio
    async def test_polling_emits_messages(self):
        """Test that polled messages are emitted."""
        channel = ConcretePollingChannel()

        received = []
        channel.on_message(lambda e: received.append(e))

        # Queue a message before starting
        event = create_message_event(
            channel_id="polling",
            message_id="msg_1",
            text="Polled message",
        )
        channel.messages_to_return.append(event)

        await channel.configure({})
        await channel.start()

        # Wait for poll
        import asyncio

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0]["text"] == "Polled message"

        await channel.stop()

    @pytest.mark.asyncio
    async def test_polling_stops(self):
        """Test that polling stops on disconnect."""
        channel = ConcretePollingChannel()
        await channel.configure({})
        await channel.start()

        import asyncio

        await asyncio.sleep(0.1)
        poll_count_at_stop = channel.poll_count

        await channel.stop()
        await asyncio.sleep(0.1)

        # Should not have polled much more
        assert channel.poll_count <= poll_count_at_stop + 1


class ConcreteWebhookChannel(WebhookChannel):
    """Concrete implementation for testing WebhookChannel."""

    def __init__(self):
        super().__init__()
        self.handled_payloads = []

    @property
    def meta(self) -> ChannelMeta:
        return {"id": "webhook", "name": "Webhook Channel", "description": ""}

    @property
    def capabilities(self) -> ChannelCapabilities:
        return {"receive_messages": True, "send_text": True}

    async def _do_connect(self) -> None:
        pass

    async def _do_disconnect(self) -> None:
        pass

    async def _do_send(self, message: ChannelMessage) -> MessageDeliveryResult:
        return MessageDeliveryResult(success=True, message_id="msg_123")

    async def handle_webhook(self, payload: dict) -> dict | None:
        self.handled_payloads.append(payload)
        return {"status": "ok"}


class TestWebhookChannel:
    """Tests for WebhookChannel."""

    def test_webhook_path(self):
        """Test default webhook path."""
        channel = ConcreteWebhookChannel()
        assert channel.webhook_path == "/webhooks/channels/webhook"

    @pytest.mark.asyncio
    async def test_handle_webhook(self):
        """Test webhook handling."""
        channel = ConcreteWebhookChannel()

        payload = {"type": "message", "text": "Hello"}
        result = await channel.handle_webhook(payload)

        assert result == {"status": "ok"}
        assert len(channel.handled_payloads) == 1

    def test_validate_webhook_signature_default(self):
        """Test default signature validation (allows all)."""
        channel = ConcreteWebhookChannel()
        result = channel.validate_webhook_signature(b"payload", "signature")
        assert result is True
