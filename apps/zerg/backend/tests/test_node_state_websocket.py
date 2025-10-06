"""Test that NODE_STATE_CHANGED events are broadcast to the correct WS topic."""

import pytest

from zerg.events import EventType
from zerg.events import event_bus
from zerg.websocket.manager import topic_manager as global_topic_manager


class DummyWebSocket:
    """Minimal stub that records frames sent by `send_json`."""

    def __init__(self):
        self.sent_frames: list[dict] = []

    async def send_json(self, payload):  # noqa: D401 – same signature as starlette.WebSocket
        # Simulate the async behaviour – immediately record the frame
        self.sent_frames.append(payload)


@pytest.mark.asyncio
async def test_node_state_changed_broadcast(monkeypatch):
    """Publishing NODE_STATE_CHANGED should broadcast on `workflow_execution:{id}` topic."""

    # ------------------------------------------------------------------
    # Arrange – fake WebSocket subscribed to the expected topic
    # ------------------------------------------------------------------
    exec_id = 123
    topic = f"workflow_execution:{exec_id}"

    dummy_ws = DummyWebSocket()

    # Monkey-patch the connection manager maps directly (skip formal connect
    # handshake because we only test broadcast logic).
    client_id = "client-test"

    async with global_topic_manager._lock:  # pylint: disable=protected-access
        global_topic_manager.active_connections[client_id] = dummy_ws  # type: ignore[arg-type]
        global_topic_manager.topic_subscriptions.setdefault(topic, set()).add(client_id)
        global_topic_manager.client_topics.setdefault(client_id, set()).add(topic)

    # ------------------------------------------------------------------
    # Act – publish event on the bus
    # ------------------------------------------------------------------
    payload = {
        "execution_id": exec_id,
        "node_id": "node_1",
        "status": "running",
        "output": None,
        "error": None,
        "event_type": EventType.NODE_STATE_CHANGED,
    }

    await event_bus.publish(EventType.NODE_STATE_CHANGED, payload)

    # ------------------------------------------------------------------
    # Assert – dummy WS received exactly one frame with expected shape
    # ------------------------------------------------------------------
    assert len(dummy_ws.sent_frames) == 1
    frame = dummy_ws.sent_frames[0]
    assert frame["type"] == "node_state"
    assert frame["data"]["execution_id"] == exec_id
    assert frame["data"]["node_id"] == "node_1"

    # Cleanup – remove dummy connection from manager maps so other tests not affected
    async with global_topic_manager._lock:  # pylint: disable=protected-access
        global_topic_manager.active_connections.pop(client_id, None)
        global_topic_manager.client_topics.pop(client_id, None)
        global_topic_manager.topic_subscriptions.get(topic, set()).discard(client_id)
