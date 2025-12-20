import logging

import pytest

from zerg.generated.ws_messages import MessageType

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.fixture
def test_thread(db_session, sample_agent):
    """Create a test thread for WebSocket testing"""
    from zerg.models.models import Thread

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


class TestChatMessageFlow:
    """Test suite for verifying chat message flow through websockets"""

    def test_basic_connection(self, ws_client):
        """Verify basic websocket connection works"""
        # Send a ping to check connectivity using envelope format
        ws_client.send_json({"type": "ping", "timestamp": 123456789, "message_id": "test-ping-1"})
        response = ws_client.receive_json()
        # Check envelope format
        assert response["v"] == 1
        assert response["type"] == "pong"
        assert "data" in response
        # Check pong payload
        assert response["data"].get("timestamp") == 123456789

    def test_chat_message_flow(self, ws_client, test_thread):
        """Test the complete flow of sending a chat message and receiving response"""
        logger.info("Starting chat message flow test")

        # Note: subscribe_thread is deprecated - streaming is automatic to user:{user_id}
        # Just send message directly
        message = {
            "type": MessageType.SEND_MESSAGE,
            "thread_id": test_thread.id,
            "content": "Test message",
            "message_id": "test-msg-1",
        }
        logger.info(f"Sending message: {message}")
        ws_client.send_json(message)

        # Wait for response
        raw_response = ws_client.receive_json()
        logger.info(f"Received response: {raw_response}")

        # Check for error on raw envelope layer
        if raw_response["type"] == "error":
            logger.error(f"Error sending message: {raw_response}")
            assert False, f"Error sending message: {raw_response.get('data', {}).get('error')}"

        # Check envelope format
        assert raw_response["type"] == MessageType.THREAD_MESSAGE
        assert "data" in raw_response
        # Check message data
        response_data = raw_response["data"]
        assert "message" in response_data
        assert "content" in response_data["message"]

    def test_multiple_messages(self, ws_client, test_thread):
        """Test sending multiple messages in sequence"""
        # Note: subscribe_thread is deprecated - streaming is automatic to user:{user_id}

        # Send multiple messages
        messages = ["First test message", "Second test message", "Third test message"]

        for idx, content in enumerate(messages):
            message = {
                "type": MessageType.SEND_MESSAGE,
                "thread_id": test_thread.id,
                "content": content,
                "message_id": f"test-msg-{idx + 1}",
            }
            logger.info(f"Sending message {idx + 1}: {message}")
            ws_client.send_json(message)

            # Verify response for each message
            raw = ws_client.receive_json()
            logger.info(f"Received response for message {idx + 1}: {raw}")

            # Check for error
            if raw["type"] == "error":
                logger.error(f"Error response for message {idx + 1}: {raw}")
                assert False, f"Error sending message {idx + 1}: {raw.get('data', {}).get('error')}"

            # Check envelope format
            assert raw["type"] == MessageType.THREAD_MESSAGE
            assert "data" in raw
            response_data = raw["data"]
            assert response_data["message"]["content"] == content
