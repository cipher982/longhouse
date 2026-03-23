"""Tests for the MockChannel plugin."""

from __future__ import annotations

import asyncio

import pytest

from zerg.channels.plugins.mock import MockChannel
from zerg.channels.types import ChannelConfig
from zerg.channels.types import ChannelMessage
from zerg.channels.types import ChannelStatus


class TestMockChannelMeta:
    """Tests for MockChannel metadata."""

    def test_meta_id(self):
        """Test channel ID."""
        channel = MockChannel()
        assert channel.meta["id"] == "mock"

    def test_meta_name(self):
        """Test channel name."""
        channel = MockChannel()
        assert channel.meta["name"] == "Mock Channel"

    def test_meta_aliases(self):
        """Test channel aliases."""
        channel = MockChannel()
        assert "test" in channel.meta["aliases"]
        assert "fake" in channel.meta["aliases"]


class TestMockChannelCapabilities:
    """Tests for MockChannel capabilities."""

    def test_send_capabilities(self):
        """Test sending capabilities."""
        channel = MockChannel()
        caps = channel.capabilities
        assert caps["send_text"] is True
        assert caps["send_media"] is True
        assert caps["send_voice"] is False

    def test_receive_capabilities(self):
        """Test receiving capabilities."""
        channel = MockChannel()
        caps = channel.capabilities
        assert caps["receive_messages"] is True

    def test_feature_capabilities(self):
        """Test feature capabilities."""
        channel = MockChannel()
        caps = channel.capabilities
        assert caps["threads"] is True
        assert caps["edit_messages"] is True
        assert caps["delete_messages"] is True


class TestMockChannelLifecycle:
    """Tests for MockChannel lifecycle methods."""

    @pytest.mark.asyncio
    async def test_initial_status(self):
        """Test initial disconnected status."""
        channel = MockChannel()
        assert channel.status == ChannelStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_configure(self):
        """Test configuration."""
        channel = MockChannel()
        config: ChannelConfig = {
            "channel_id": "mock",
            "enabled": True,
            "settings": {"delay_ms": 100},
        }
        await channel.configure(config)
        assert channel._delay_ms == 100

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Test start and stop lifecycle."""
        channel = MockChannel()
        await channel.configure({})

        # Start
        await channel.start()
        assert channel.status == ChannelStatus.CONNECTED

        # Stop
        await channel.stop()
        assert channel.status == ChannelStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager."""
        channel = MockChannel()
        await channel.configure({})

        async with channel.session():
            assert channel.status == ChannelStatus.CONNECTED

        assert channel.status == ChannelStatus.DISCONNECTED


class TestMockChannelMessaging:
    """Tests for MockChannel messaging."""

    @pytest.mark.asyncio
    async def test_send_message(self):
        """Test sending a message."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        message: ChannelMessage = {
            "channel_id": "mock",
            "to": "user123",
            "text": "Hello!",
        }
        result = await channel.send_message(message)

        assert result["success"] is True
        assert result["message_id"] is not None
        assert result["message_id"].startswith("mock_")

        await channel.stop()

    @pytest.mark.asyncio
    async def test_sent_messages_recorded(self):
        """Test that sent messages are recorded."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        await channel.send_message(ChannelMessage(channel_id="mock", to="user1", text="First"))
        await channel.send_message(ChannelMessage(channel_id="mock", to="user2", text="Second"))

        assert len(channel.sent_messages) == 2
        # Most recent first
        assert channel.sent_messages[0]["text"] == "Second"
        assert channel.sent_messages[1]["text"] == "First"

        await channel.stop()

    @pytest.mark.asyncio
    async def test_get_last_sent(self):
        """Test getting the last sent message."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        await channel.send_message(ChannelMessage(channel_id="mock", to="user1", text="Hello"))

        last = channel.get_last_sent()
        assert last is not None
        assert last["text"] == "Hello"

        await channel.stop()

    @pytest.mark.asyncio
    async def test_clear_sent_messages(self):
        """Test clearing sent messages."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        await channel.send_message(ChannelMessage(channel_id="mock", to="user1", text="Test"))
        assert len(channel.sent_messages) == 1

        channel.clear_sent_messages()
        assert len(channel.sent_messages) == 0

        await channel.stop()

    @pytest.mark.asyncio
    async def test_edit_message(self):
        """Test editing a message."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        result = await channel.edit_message("msg_123", "Updated text")
        assert result["success"] is True

        await channel.stop()

    @pytest.mark.asyncio
    async def test_delete_message(self):
        """Test deleting a message."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        result = await channel.delete_message("msg_123")
        assert result["success"] is True

        await channel.stop()

    @pytest.mark.asyncio
    async def test_send_reaction(self):
        """Test sending a reaction."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        result = await channel.send_reaction("msg_123", "thumbs_up")
        assert result["success"] is True

        await channel.stop()


class TestMockChannelTestHelpers:
    """Tests for MockChannel test helper methods."""

    @pytest.mark.asyncio
    async def test_inject_message(self):
        """Test injecting a fake incoming message."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        received_events = []
        channel.on_message(lambda e: received_events.append(e))

        event = channel.inject_message(
            text="Hello from test!",
            sender_id="test_user",
            sender_name="Test User",
        )

        assert event["text"] == "Hello from test!"
        assert event["sender_id"] == "test_user"
        assert len(received_events) == 1
        assert received_events[0]["text"] == "Hello from test!"

        await channel.stop()

    @pytest.mark.asyncio
    async def test_inject_message_with_thread(self):
        """Test injecting a message in a thread."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        event = channel.inject_message(
            text="Thread reply",
            sender_id="user1",
            thread_id="thread_123",
            reply_to_id="msg_456",
        )

        assert event["thread_id"] == "thread_123"
        assert event["reply_to_id"] == "msg_456"

        await channel.stop()

    @pytest.mark.asyncio
    async def test_set_fail_next_send(self):
        """Test configuring send to fail."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        channel.set_fail_next_send()
        result = await channel.send_message(ChannelMessage(channel_id="mock", to="user1", text="Test"))

        assert result["success"] is False
        assert result["error_code"] == "MOCK_FAILURE"

        # Next send should succeed
        result2 = await channel.send_message(ChannelMessage(channel_id="mock", to="user1", text="Test2"))
        assert result2["success"] is True

        await channel.stop()

    @pytest.mark.asyncio
    async def test_set_delay(self):
        """Test setting simulated delay."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        channel.set_delay(50)  # 50ms

        start = asyncio.get_event_loop().time()
        await channel.send_message(ChannelMessage(channel_id="mock", to="user1", text="Test"))
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed >= 0.04  # Allow some tolerance

        await channel.stop()

    @pytest.mark.asyncio
    async def test_auto_reply(self):
        """Test auto-reply feature."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        received_events = []
        channel.on_message(lambda e: received_events.append(e))

        channel.set_auto_reply("Thanks for your message!")

        await channel.send_message(ChannelMessage(channel_id="mock", to="user1", text="Hello"))

        # Wait for auto-reply
        await asyncio.sleep(0.02)

        assert len(received_events) == 1
        assert received_events[0]["text"] == "Thanks for your message!"

        await channel.stop()


class TestMockChannelEventHandlers:
    """Tests for MockChannel event handlers."""

    @pytest.mark.asyncio
    async def test_on_message_handler(self):
        """Test message handler registration."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        events = []
        unsubscribe = channel.on_message(lambda e: events.append(e))

        channel.inject_message(text="Test 1")
        channel.inject_message(text="Test 2")

        assert len(events) == 2

        # Unsubscribe
        unsubscribe()
        channel.inject_message(text="Test 3")

        assert len(events) == 2  # No new events

        await channel.stop()

    @pytest.mark.asyncio
    async def test_multiple_handlers(self):
        """Test multiple message handlers."""
        channel = MockChannel()
        await channel.configure({})
        await channel.start()

        events1 = []
        events2 = []

        channel.on_message(lambda e: events1.append(e))
        channel.on_message(lambda e: events2.append(e))

        channel.inject_message(text="Test")

        assert len(events1) == 1
        assert len(events2) == 1

        await channel.stop()

    @pytest.mark.asyncio
    async def test_on_status_change(self):
        """Test status change handler."""
        channel = MockChannel()

        statuses = []
        channel.on_status_change(lambda s: statuses.append(s))

        await channel.configure({})
        await channel.start()

        assert ChannelStatus.CONNECTING in statuses
        assert ChannelStatus.CONNECTED in statuses

        await channel.stop()
        assert ChannelStatus.DISCONNECTED in statuses


class TestMockChannelConfigSchema:
    """Tests for MockChannel configuration schema."""

    def test_config_schema_fields(self):
        """Test configuration schema has expected fields."""
        channel = MockChannel()
        schema = channel.config_schema

        assert schema is not None
        field_keys = [f["key"] for f in schema["fields"]]
        assert "enabled" in field_keys
        assert "delay_ms" in field_keys
        assert "auto_reply" in field_keys

    def test_validate_config(self):
        """Test configuration validation."""
        channel = MockChannel()

        # Valid config (no required fields)
        errors = channel.validate_config({})
        assert len(errors) == 0
