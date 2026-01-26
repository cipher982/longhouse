"""Unit tests for the TopicConnectionManager."""

from unittest.mock import AsyncMock

import pytest

from zerg.events import EventType
from zerg.generated.ws_messages import Envelope
from zerg.websocket.manager import TopicConnectionManager


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket connection."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.fixture
def topic_manager():
    """Create a fresh topic manager for each test."""
    return TopicConnectionManager()


@pytest.mark.asyncio
class TestTopicConnectionManager:
    """Test suite for TopicConnectionManager."""

    async def test_connect_disconnect(self, topic_manager, mock_websocket):
        """Test basic connection and disconnection."""
        client_id = "test-client-1"

        # Test connect
        await topic_manager.connect(client_id, mock_websocket)
        assert client_id in topic_manager.active_connections
        assert client_id in topic_manager.client_topics
        assert not topic_manager.client_topics[client_id]  # Empty set initially

        # Test disconnect
        await topic_manager.disconnect(client_id)
        assert client_id not in topic_manager.active_connections
        assert client_id not in topic_manager.client_topics

    async def test_topic_subscription(self, topic_manager, mock_websocket):
        """Test subscribing and unsubscribing from topics."""
        client_id = "test-client-1"
        topic = "thread:123"

        # Connect and subscribe
        await topic_manager.connect(client_id, mock_websocket)
        await topic_manager.subscribe_to_topic(client_id, topic)

        # Verify subscription
        assert topic in topic_manager.topic_subscriptions
        assert client_id in topic_manager.topic_subscriptions[topic]
        assert topic in topic_manager.client_topics[client_id]

        # Unsubscribe
        await topic_manager.unsubscribe_from_topic(client_id, topic)

        # Verify unsubscription
        assert topic not in topic_manager.topic_subscriptions
        assert topic not in topic_manager.client_topics[client_id]

    async def test_multiple_clients_same_topic(self, topic_manager):
        """Test multiple clients subscribing to the same topic."""
        topic = "fiche:456"
        client1_ws = AsyncMock()
        client2_ws = AsyncMock()

        # Connect and subscribe both clients
        await topic_manager.connect("client1", client1_ws)
        await topic_manager.connect("client2", client2_ws)
        await topic_manager.subscribe_to_topic("client1", topic)
        await topic_manager.subscribe_to_topic("client2", topic)

        # Broadcast a message using envelope format
        envelope = Envelope.create(message_type="test", topic=topic, data={"content": "hello"})
        await topic_manager.broadcast_to_topic(topic, envelope.model_dump())

        # Verify both clients received **one** message each (payload now wrapped)
        client1_ws.send_json.assert_called_once()
        client2_ws.send_json.assert_called_once()

    async def test_client_multiple_topics(self, topic_manager, mock_websocket):
        """Test a client subscribing to multiple topics."""
        client_id = "test-client-1"
        topics = ["thread:123", "fiche:456", "thread:789"]

        # Connect and subscribe to multiple topics
        await topic_manager.connect(client_id, mock_websocket)
        for topic in topics:
            await topic_manager.subscribe_to_topic(client_id, topic)

        # Verify all subscriptions
        for topic in topics:
            assert topic in topic_manager.topic_subscriptions
            assert client_id in topic_manager.topic_subscriptions[topic]
            assert topic in topic_manager.client_topics[client_id]

        # Disconnect
        await topic_manager.disconnect(client_id)

        # Verify all subscriptions are cleaned up
        for topic in topics:
            assert topic not in topic_manager.topic_subscriptions
        assert client_id not in topic_manager.client_topics

    async def test_handle_fiche_event(self, topic_manager, mock_websocket):
        """Test handling of fiche events."""
        client_id = "test-client-1"
        fiche_id = 123
        topic = f"fiche:{fiche_id}"

        # Connect and subscribe
        await topic_manager.connect(client_id, mock_websocket)
        await topic_manager.subscribe_to_topic(client_id, topic)

        # Simulate fiche event
        event_data = {"id": fiche_id, "name": "Test Fiche", "event_type": EventType.FICHE_UPDATED}
        await topic_manager._handle_fiche_event(event_data)

        # Verify message was sent
        mock_websocket.send_json.assert_called_once()
        sent_envelope = mock_websocket.send_json.call_args[0][0]
        assert sent_envelope["type"] == EventType.FICHE_UPDATED
        # event_type should be removed from data to prevent duplication in envelope
        expected_data = {"id": fiche_id, "name": "Test Fiche"}
        assert sent_envelope["data"] == expected_data

    async def test_handle_thread_event(self, topic_manager, mock_websocket):
        """Test handling of thread events."""
        client_id = "test-client-1"
        thread_id = 456
        topic = f"thread:{thread_id}"

        # Connect and subscribe
        await topic_manager.connect(client_id, mock_websocket)
        await topic_manager.subscribe_to_topic(client_id, topic)

        # Simulate thread event
        event_data = {
            "thread_id": thread_id,
            "message": {"content": "test"},
            "event_type": EventType.THREAD_MESSAGE_CREATED,
        }
        await topic_manager._handle_thread_event(event_data)

        # Verify message was sent
        mock_websocket.send_json.assert_called_once()
        sent_env = mock_websocket.send_json.call_args[0][0]
        assert sent_env["type"] == EventType.THREAD_MESSAGE_CREATED
        # event_type should be removed from data to prevent duplication in envelope
        expected_data = {
            "thread_id": thread_id,
            "message": {"content": "test"},
        }
        assert sent_env["data"] == expected_data

    async def test_cleanup_on_send_failure(self, topic_manager):
        """Test cleanup when sending to a client fails."""
        topic = "thread:123"

        # Create two clients, one that will fail
        good_client_ws = AsyncMock()
        bad_client_ws = AsyncMock()
        bad_client_ws.send_json.side_effect = Exception("Connection lost")

        # Connect and subscribe both clients
        await topic_manager.connect("good_client", good_client_ws)
        await topic_manager.connect("bad_client", bad_client_ws)
        await topic_manager.subscribe_to_topic("good_client", topic)
        await topic_manager.subscribe_to_topic("bad_client", topic)

        # Broadcast a message using envelope format
        envelope = Envelope.create(message_type="test", topic=topic, data={"content": "hello"})
        await topic_manager.broadcast_to_topic(topic, envelope.model_dump())

        # Verify good client got **one** message (payload now enveloped)
        good_client_ws.send_json.assert_called_once()

        # Verify bad client was cleaned up
        assert "bad_client" not in topic_manager.active_connections
        assert "bad_client" not in topic_manager.client_topics
        assert "bad_client" not in topic_manager.topic_subscriptions[topic]
