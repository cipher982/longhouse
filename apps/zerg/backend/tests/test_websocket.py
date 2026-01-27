import pytest

"""Tests for the new WebSocket implementation."""

import logging

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from zerg.events import EventType
from zerg.events import event_bus
from zerg.models.models import Fiche
from zerg.models.models import Thread
from zerg.websocket.manager import TopicConnectionManager

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@pytest.fixture
def topic_manager():
    """Create a fresh topic manager for each test."""
    return TopicConnectionManager()


@pytest.fixture
def ws_client(test_client: TestClient) -> WebSocketTestSession:
    """Create a WebSocket test client for the /api/ws endpoint."""
    with test_client.websocket_connect("/api/ws") as websocket:
        yield websocket


@pytest.mark.asyncio
@pytest.mark.skip(reason="Temporarily disabled due to hangs and logging issues")
class TestTopicBasedWebSocket:
    """Test suite for topic-based WebSocket functionality."""

    def test_basic_connection(self, ws_client: WebSocketTestSession):
        """Test basic WebSocket connection and ping/pong."""
        # Send a ping
        ws_client.send_json({"type": "ping", "timestamp": 123456789, "message_id": "test-ping-1"})

        # Expect pong response
        response = ws_client.receive_json()
        assert response["type"] == "pong"
        assert "timestamp" in response

    async def test_subscribe_to_thread(self, ws_client, sample_thread: Thread):
        """Test subscribing to a thread topic."""
        # Subscribe to thread
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": [f"thread:{sample_thread.id}"],
                "message_id": "test-sub-1",
            }
        )

        # Should receive thread history
        response = ws_client.receive_json()
        assert response["type"] == "thread_history"
        assert response["thread_id"] == sample_thread.id
        assert "messages" in response

        # Publish a thread event
        await event_bus.publish(
            EventType.THREAD_MESSAGE_CREATED,
            {
                "thread_id": sample_thread.id,
                "message": {"content": "Test message", "role": "user"},
            },
        )

        # Should receive the event
        response = ws_client.receive_json()
        assert response["type"] == "thread_event"
        assert response["data"]["thread_id"] == sample_thread.id

    async def test_subscribe_to_fiche(self, ws_client, sample_fiche: Fiche):
        """Test subscribing to an fiche topic."""
        logger.info("START: test_subscribe_to_fiche")
        # Subscribe to fiche
        logger.info(f"Subscribing to fiche: {sample_fiche.id}")
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": [f"fiche:{sample_fiche.id}"],
                "message_id": "test-sub-2",
            }
        )
        logger.info("Subscription message sent")

        # Should receive current fiche state
        logger.info("Waiting for fiche_state response...")
        response = ws_client.receive_json()
        logger.info(f"Received fiche_state response: {response}")
        assert response["type"] == "fiche_state"
        assert response["data"]["id"] == sample_fiche.id
        assert response["data"]["name"] == sample_fiche.name
        logger.info("Fiche state assertion passed")

        # Publish an fiche event
        event_data = {
            "id": sample_fiche.id,
            "name": sample_fiche.name,
            "status": "processing",
        }
        logger.info(f"Publishing FICHE_UPDATED event: {event_data}")
        await event_bus.publish(EventType.FICHE_UPDATED, event_data)
        logger.info("FICHE_UPDATED event published")

        # Should receive the event
        logger.info("Waiting for fiche_event response...")
        response = ws_client.receive_json()
        logger.info(f"Received fiche_event response: {response}")
        assert response["type"] == "fiche_event"
        assert response["data"]["id"] == sample_fiche.id
        assert response["data"]["status"] == "processing"
        logger.info("Fiche event assertion passed")
        logger.info("END: test_subscribe_to_fiche")

    async def test_subscribe_to_multiple_topics(self, ws_client, sample_thread: Thread, sample_fiche: Fiche):
        """Test subscribing to multiple topics in one request."""
        # Subscribe to both thread and fiche
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": [f"thread:{sample_thread.id}", f"fiche:{sample_fiche.id}"],
                "message_id": "test-sub-3",
            }
        )

        # Should receive thread history and fiche state
        responses = [ws_client.receive_json(), ws_client.receive_json()]

        # Verify we got both types of responses (order may vary)
        response_types = {r["type"] for r in responses}
        assert "thread_history" in response_types
        assert "fiche_state" in response_types

    async def test_unsubscribe(
        self,
        ws_client: WebSocketTestSession,
        sample_thread: Thread,
        topic_manager: TopicConnectionManager,
    ):
        """Test unsubscribing from a topic."""
        # First subscribe
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": [f"thread:{sample_thread.id}"],
                "message_id": "test-sub-4",
            }
        )
        ws_client.receive_json()  # Consume history response

        # Then unsubscribe
        ws_client.send_json(
            {
                "type": "unsubscribe",
                "topics": [f"thread:{sample_thread.id}"],
                "message_id": "test-unsub-1",
            }
        )

        # Wait for unsubscribe confirmation
        response = ws_client.receive_json()
        assert response["type"] == "unsubscribe_success"

        # Verify the topic subscription was removed
        topic = f"thread:{sample_thread.id}"
        assert (
            topic not in topic_manager.topic_subscriptions or not topic_manager.topic_subscriptions[topic]
        ), "Topic should have no subscribers after unsubscribe"

    async def test_invalid_topic_format(self, ws_client):
        """Test handling of invalid topic formats."""
        # Try to subscribe to invalid topic
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": ["invalid:123", "also:invalid"],
                "message_id": "test-invalid-1",
            }
        )

        # Should receive error for each invalid topic
        response = ws_client.receive_json()
        assert response["type"] == "error"
        assert "Invalid topic format" in response["error"]

    async def test_nonexistent_resources(self, ws_client):
        """Test subscribing to non-existent thread/fiche."""
        # Try to subscribe to non-existent resources
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": ["thread:99999", "fiche:99999"],
                "message_id": "test-invalid-2",
            }
        )

        # Should receive error messages
        response1 = ws_client.receive_json()
        response2 = ws_client.receive_json()

        assert response1["type"] == "error"
        assert response2["type"] == "error"
        assert any("Thread 99999 not found" in r["error"] for r in [response1, response2])
        assert any("Fiche 99999 not found" in r["error"] for r in [response1, response2])

    def test_initial_topics_parameter(self, test_client: TestClient, sample_thread: Thread):
        """Test connecting with initial topics parameter."""
        # Create a client with initial topics
        with test_client.websocket_connect(f"/api/ws?initial_topics=thread:{sample_thread.id}") as websocket:
            # Should automatically receive thread history
            response = websocket.receive_json()
            print(f"DEBUG: Response received: {response}")
            assert response["type"] == "thread_history"
            assert response["thread_id"] == sample_thread.id
