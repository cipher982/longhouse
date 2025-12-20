import logging

import pytest

from zerg.main import app
from zerg.models.models import Thread

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.mark.skip(reason="Temporarily disabled due to hangs and logging issues")
def test_websocket_endpoint_exists():
    """Verify that the WebSocket endpoint exists in the app"""
    ws_routes = [
        route for route in app.routes if route.path == "/api/ws" and "websocket" in str(route.endpoint).lower()
    ]
    assert len(ws_routes) > 0, "WebSocket endpoint for /api/ws is not registered"


@pytest.fixture
def test_thread(db_session, sample_agent):
    """Create a test thread for WebSocket testing"""
    thread = Thread(
        agent_id=sample_agent.id,
        title="WebSocket Test Thread",
        active=True,
        memory_strategy="buffer",
    )
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)
    logger.info(f"Created test thread with ID: {thread.id}")
    return thread


@pytest.fixture
def ws_client(client):
    """Create a WebSocket test client for a single connection"""
    logger.info("Connecting to WebSocket at /api/ws")

    with client.websocket_connect("/api/ws") as websocket:
        yield websocket


@pytest.mark.skip(reason="Temporarily disabled due to hangs and logging issues")
class TestWebSocketIntegration:
    """Integration tests for WebSocket functionality"""

    def test_connect_disconnect(self, ws_client):
        """Test basic connection and disconnection"""
        # Send a ping to check connectivity
        ws_client.send_json({"type": "ping", "timestamp": 123456789})
        response = ws_client.receive_json()
        assert response["type"] == "pong"
        assert "timestamp" in response

    def test_subscribe_thread(self, ws_client, test_thread):
        """Test subscribing to a thread"""
        # Subscribe to thread topic with explicit thread ID
        logger.info(f"Subscribing to thread: {test_thread.id}")
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": [f"thread:{test_thread.id}"],
                "message_id": "test-sub-1",
            }
        )

        # Should receive thread history
        response = ws_client.receive_json()
        logger.info(f"Received response: {response}")

        # If we got an error, log it and fail with details
        if response.get("type") == "error":
            logger.error(f"Error response: {response}")
            assert False, f"Error subscribing to thread: {response.get('error')}"

        assert response["type"] == "thread_history"
        assert response["thread_id"] == test_thread.id
        assert "messages" in response

    def test_subscribe_invalid_thread(self, ws_client):
        """Test subscribing to a non-existent thread"""
        # Use a high ID that's unlikely to exist
        invalid_thread_id = 999999
        logger.info(f"Attempting to subscribe to invalid thread: {invalid_thread_id}")
        ws_client.send_json(
            {
                "type": "subscribe",
                "topics": [f"thread:{invalid_thread_id}"],
                "message_id": "test-sub-2",
            }
        )

        response = ws_client.receive_json()
        logger.info(f"Received response: {response}")
        assert response["type"] == "error"
        assert f"Thread {invalid_thread_id} not found" in response["error"]

    def test_send_message(self, ws_client, test_thread):
        """Test sending a message to a thread"""
        # First subscribe to thread
        logger.info(f"Subscribing to thread: {test_thread.id}")
        ws_client.send_json(
            {
                "type": "subscribe_thread",
                "thread_id": test_thread.id,
                "message_id": "test-sub-3",
            }
        )
        history = ws_client.receive_json()  # Consume history
        logger.info(f"Received history: {history}")

        # Check for error
        if history["type"] == "error":
            logger.error(f"Error subscribing to thread: {history}")
            assert False, f"Error subscribing to thread: {history.get('error')}"

        # Send test message
        test_content = "Hello, WebSocket world!"
        logger.info(f"Sending message to thread {test_thread.id}: {test_content}")
        ws_client.send_json(
            {
                "type": "send_message",
                "thread_id": test_thread.id,
                "content": test_content,
                "message_id": "test-msg-1",
            }
        )

        # Should receive a broadcast message
        response = ws_client.receive_json()
        logger.info(f"Received response: {response}")

        # Check for error
        if response["type"] == "error":
            logger.error(f"Error sending message: {response}")
            assert False, f"Error sending message: {response.get('error')}"

        assert response["type"] == "thread_message"
        assert response["thread_id"] == test_thread.id
        assert response["message"]["content"] == test_content

    def test_multiple_clients(self, client, test_thread):
        """Test message broadcasting to multiple clients subscribed to same thread"""
        # Connect two websocket clients
        with client.websocket_connect("/api/ws") as client1, client.websocket_connect("/api/ws") as client2:
            clients = [client1, client2]

            logger.info(f"Subscribing both clients to thread: {test_thread.id}")
            # Subscribe both clients to the same thread
            for i, ws in enumerate(clients):
                ws.send_json(
                    {
                        "type": "subscribe_thread",
                        "thread_id": test_thread.id,
                        "message_id": f"test-sub-{i}",
                    }
                )
                history = ws.receive_json()  # Consume history
                logger.info(f"Client {i} received history: {history}")

                # Check for error
                if history["type"] == "error":
                    logger.error(f"Client {i} error subscribing to thread: {history}")
                    assert False, f"Client {i} error subscribing to thread: {history.get('error')}"

            # Client 1 sends a message
            test_content = "Broadcast test"
            logger.info(f"Client 0 sending message to thread {test_thread.id}: {test_content}")
            clients[0].send_json(
                {
                    "type": "send_message",
                    "thread_id": test_thread.id,
                    "content": test_content,
                    "message_id": "test-msg-2",
                }
            )

            # Get responses from both clients
            responses = []
            for i, ws in enumerate(clients):
                response = ws.receive_json()
                logger.info(f"Client {i} received response: {response}")

                # Check for error
                if response["type"] == "error":
                    logger.error(f"Client {i} error receiving message: {response}")
                    assert False, f"Client {i} error: {response.get('error')}"

                responses.append(response)

            # Verify both clients received valid messages
            for response in responses:
                assert response["type"] == "thread_message"
                assert response["message"]["content"] == test_content
