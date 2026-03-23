"""Tests for the ChannelRouter."""

from __future__ import annotations

import asyncio

import pytest

from zerg.channels.plugins.mock import MockChannel
from zerg.channels.registry import ChannelRegistry
from zerg.channels.router import ChannelRouter
from zerg.channels.router import ConversationContext
from zerg.channels.types import ChannelMessage


@pytest.fixture
def registry():
    """Create a fresh registry for testing."""
    return ChannelRegistry()


@pytest.fixture
def mock_channel():
    """Create a configured mock channel."""
    channel = MockChannel()
    return channel


@pytest.fixture
async def router(registry, mock_channel):
    """Create a router with a mock channel."""
    asyncio.get_event_loop()
    await mock_channel.configure({})
    registry.register(mock_channel)
    router = ChannelRouter(registry)
    yield router
    await router.stop()


class TestRouterLifecycle:
    """Tests for router lifecycle."""

    @pytest.mark.asyncio
    async def test_start_stop(self, registry, mock_channel):
        """Test router start and stop."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()
        assert router._running is True

        await router.stop()
        assert router._running is False

        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_start_subscribes_to_channels(self, registry, mock_channel):
        """Test that start subscribes to channel messages."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        # Should have unsubscribe functions registered
        assert len(router._unsubscribe_fns) > 0

        await router.stop()
        await mock_channel.stop()


class TestRouterSending:
    """Tests for sending messages through the router."""

    @pytest.mark.asyncio
    async def test_send_message(self, registry, mock_channel):
        """Test sending a message."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        result = await router.send(
            "mock",
            to="user123",
            text="Hello!",
        )

        assert result["success"] is True
        assert mock_channel.get_last_sent()["text"] == "Hello!"

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_send_message_object(self, registry, mock_channel):
        """Test sending a message object."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        message = ChannelMessage(
            channel_id="mock",
            to="user123",
            text="Hello from object!",
        )
        result = await router.send_message(message)

        assert result["success"] is True

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_send_to_unknown_channel(self, registry):
        """Test sending to unknown channel."""
        router = ChannelRouter(registry)

        result = await router.send(
            "nonexistent",
            to="user123",
            text="Hello!",
        )

        assert result["success"] is False
        assert result["error_code"] == "CHANNEL_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_broadcast(self, registry):
        """Test broadcasting to multiple channels."""
        channel1 = MockChannel()
        await channel1.configure({})
        await channel1.start()

        # Create a second mock channel with different ID
        channel2 = MockChannel()
        channel2._meta_override = {"id": "mock2", "name": "Mock 2"}
        # Override meta property
        original_meta = channel2.meta
        channel2.meta = {**original_meta, "id": "mock2", "name": "Mock 2", "aliases": []}

        await channel2.configure({})
        await channel2.start()

        registry.register(channel1)
        registry.register(channel2)

        router = ChannelRouter(registry)
        await router.start()

        message = ChannelMessage(
            to="broadcast_user",
            text="Broadcast message",
        )
        results = await router.broadcast(message, channels=["mock", "mock2"])

        assert results["mock"]["success"] is True
        assert results["mock2"]["success"] is True

        await router.stop()
        await channel1.stop()
        await channel2.stop()


class TestRouterInbound:
    """Tests for handling inbound messages."""

    @pytest.mark.asyncio
    async def test_inbound_handler(self, registry, mock_channel):
        """Test inbound message handler."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        received = []
        router.on_inbound(lambda event, ctx: received.append((event, ctx)))

        # Inject a message
        mock_channel.inject_message(
            text="Incoming!",
            sender_id="user456",
        )

        assert len(received) == 1
        event, ctx = received[0]
        assert event["text"] == "Incoming!"
        assert isinstance(ctx, ConversationContext)

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_conversation_context_created(self, registry, mock_channel):
        """Test that conversation context is created."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        contexts = []
        router.on_inbound(lambda event, ctx: contexts.append(ctx))

        mock_channel.inject_message(
            text="Hello",
            sender_id="user123",
            chat_id="chat_xyz",
        )

        assert len(contexts) == 1
        ctx = contexts[0]
        assert ctx.channel_id == "mock"
        assert ctx.chat_id == "chat_xyz"

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_conversation_context_reused(self, registry, mock_channel):
        """Test that conversation context is reused."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        contexts = []
        router.on_inbound(lambda event, ctx: contexts.append(ctx))

        # Send two messages from same chat
        mock_channel.inject_message(text="First", sender_id="user1", chat_id="chat_a")
        mock_channel.inject_message(text="Second", sender_id="user1", chat_id="chat_a")

        assert len(contexts) == 2
        # Same conversation context object
        assert contexts[0].conversation_id == contexts[1].conversation_id

        await router.stop()
        await mock_channel.stop()


class TestRouterFilters:
    """Tests for outbound message filters."""

    @pytest.mark.asyncio
    async def test_outbound_filter_modifies_message(self, registry, mock_channel):
        """Test that outbound filter can modify message."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        # Add filter that uppercases text
        def uppercase_filter(msg: ChannelMessage) -> ChannelMessage:
            if msg.get("text"):
                msg["text"] = msg["text"].upper()
            return msg

        router.add_outbound_filter(uppercase_filter)

        await router.send("mock", to="user1", text="hello")

        assert mock_channel.get_last_sent()["text"] == "HELLO"

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_outbound_filter_blocks_message(self, registry, mock_channel):
        """Test that outbound filter can block message."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        # Add filter that blocks messages with "spam"
        def spam_filter(msg: ChannelMessage) -> ChannelMessage | None:
            if msg.get("text") and "spam" in msg["text"].lower():
                return None  # Block
            return msg

        router.add_outbound_filter(spam_filter)

        result = await router.send("mock", to="user1", text="Buy spam now!")

        assert result["success"] is False
        assert result["error_code"] == "FILTERED"
        assert mock_channel.get_last_sent() is None

        await router.stop()
        await mock_channel.stop()


class TestRouterConversations:
    """Tests for conversation management."""

    @pytest.mark.asyncio
    async def test_get_conversation(self, registry, mock_channel):
        """Test getting a conversation."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        router.on_inbound(lambda e, c: None)  # Need handler to process
        mock_channel.inject_message(text="Hi", sender_id="user1", chat_id="chat_123")

        conv = router.get_conversation("mock:chat_123")
        assert conv is not None
        assert conv.chat_id == "chat_123"

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_list_conversations(self, registry, mock_channel):
        """Test listing conversations."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        router.on_inbound(lambda e, c: None)
        mock_channel.inject_message(text="Hi", sender_id="user1", chat_id="chat_1")
        mock_channel.inject_message(text="Hi", sender_id="user2", chat_id="chat_2")

        convs = router.list_conversations()
        assert len(convs) == 2

        # Filter by channel
        mock_convs = router.list_conversations(channel_id="mock")
        assert len(mock_convs) == 2

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_clear_conversation(self, registry, mock_channel):
        """Test clearing a conversation."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        router.on_inbound(lambda e, c: None)
        mock_channel.inject_message(text="Hi", sender_id="user1", chat_id="chat_123")

        assert router.get_conversation("mock:chat_123") is not None

        result = router.clear_conversation("mock:chat_123")
        assert result is True
        assert router.get_conversation("mock:chat_123") is None

        await router.stop()
        await mock_channel.stop()


class TestRouterHistory:
    """Tests for message history."""

    @pytest.mark.asyncio
    async def test_message_history_recorded(self, registry, mock_channel):
        """Test that messages are recorded in history."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        # Send outbound
        await router.send("mock", to="user1", text="Hello")

        # Receive inbound
        router.on_inbound(lambda e, c: None)
        mock_channel.inject_message(text="Hi back", sender_id="user1")

        history = router.get_message_history()
        assert len(history) == 2

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_message_history_filtered(self, registry, mock_channel):
        """Test filtering message history."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        await router.send("mock", to="user1", text="Out 1")
        await router.send("mock", to="user1", text="Out 2")

        router.on_inbound(lambda e, c: None)
        mock_channel.inject_message(text="In 1", sender_id="user1")

        # Filter by direction
        outbound = router.get_message_history(direction="outbound")
        assert len(outbound) == 2

        inbound = router.get_message_history(direction="inbound")
        assert len(inbound) == 1

        await router.stop()
        await mock_channel.stop()

    @pytest.mark.asyncio
    async def test_message_history_limit(self, registry, mock_channel):
        """Test message history limit."""
        await mock_channel.configure({})
        await mock_channel.start()
        registry.register(mock_channel)

        router = ChannelRouter(registry)
        await router.start()

        for i in range(10):
            await router.send("mock", to="user1", text=f"Message {i}")

        # Limit to 5
        history = router.get_message_history(limit=5)
        assert len(history) == 5

        # Most recent first
        assert "Message 9" in history[0].message.get("text", "")

        await router.stop()
        await mock_channel.stop()
