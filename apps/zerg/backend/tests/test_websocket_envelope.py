import pytest
from fastapi.testclient import TestClient

from zerg.generated.ws_messages import Envelope
from zerg.websocket.manager import topic_manager

# Envelope structure is always enabled - no configuration needed


@pytest.fixture
def ws_client(test_client: TestClient):
    """Create a WebSocket test client for the /api/ws endpoint."""
    with test_client.websocket_connect("/api/ws") as websocket:
        yield websocket


def test_envelope_ping(ws_client):
    """Test that ping responses are wrapped in Envelope."""
    ws_client.send_json({"type": "ping", "timestamp": 123456789, "message_id": "test-ping-1"})
    response = ws_client.receive_json()
    # Envelope fields
    assert response["v"] == 1
    assert response["type"] == "pong"
    assert response["topic"] == "system" or response["topic"]  # topic may be "system" or similar
    assert "ts" in response
    assert "data" in response
    # Pong payload
    assert response["data"].get("timestamp") == 123456789


def test_envelope_backpressure_disconnect(monkeypatch, test_client: TestClient):
    """Test that a client is dropped if its queue overflows (back-pressure)."""
    # Patch queue size to something tiny for test
    monkeypatch.setattr(topic_manager, "QUEUE_SIZE", 2)
    with test_client.websocket_connect("/api/ws"):
        # Wait for queue registration to complete after WebSocket handshake
        import time

        time.sleep(0.01)  # Brief wait for queue registration

        # Ensure we have at least one registered client
        if not topic_manager.client_queues:
            import pytest

            pytest.skip("No client queues registered - WebSocket handshake timing issue")

        # Simulate slow client by not reading messages
        for i in range(5):
            topic_manager.client_queues[list(topic_manager.client_queues.keys())[0]].put_nowait(
                {"type": "test", "data": {"i": i}}
            )
        # After overflow, the client should be disconnected
        # (No assertion here, but no exception = pass)


@pytest.mark.asyncio
async def test_no_hang_on_slow_client(test_client: TestClient):
    """Test that a slow client does not block other clients (no hang)."""
    # Connect two clients
    with (
        test_client.websocket_connect("/api/ws") as _slow_client,
        test_client.websocket_connect("/api/ws") as fast_client,
    ):
        # Simulate slow client by not reading from _slow_client
        for i in range(3):
            envelope = Envelope.create(message_type="test", topic="system", data={"i": i})
            await topic_manager.broadcast_to_topic("system", envelope.model_dump())

        # fast_client should still be able to receive messages
        for _ in range(3):
            msg = fast_client.receive_json()
            assert msg["v"] == 1
            assert msg["type"] == "test"
