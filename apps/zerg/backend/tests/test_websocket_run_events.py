import pytest

from zerg.schemas.ws_messages import MessageType


@pytest.fixture
def ws_client(client):
    """WebSocket client fixture"""
    with client.websocket_connect("/api/ws") as websocket:
        yield websocket


@pytest.mark.asyncio
async def test_run_update_ws_events(ws_client, client, sample_agent):
    """When an agent run is triggered, WebSocket subscribers receive run_update events"""
    agent_id = sample_agent.id
    # Subscribe to agent topic
    subscribe_msg = {
        "type": "subscribe",
        "topics": [f"agent:{agent_id}"],
        "message_id": "sub-run-updates",
    }
    ws_client.send_json(subscribe_msg)

    # Expect initial agent_state message
    init = ws_client.receive_json()
    assert init.get("type") == MessageType.AGENT_STATE
    assert init.get("data", {}).get("id") == agent_id

    # Trigger a manual run via the task endpoint
    resp = client.post(f"/api/agents/{agent_id}/task")
    assert resp.status_code == 202

    # Collect statuses from run_update events
    statuses = set()
    # We expect queued, running, and success events
    for _ in range(5):
        msg = ws_client.receive_json()
        if msg.get("type") != "run_update":
            continue
        data = msg.get("data", {})
        status = data.get("status")
        if status:
            statuses.add(status)
        if statuses >= {"queued", "running", "success"}:
            break

    assert "queued" in statuses
    assert "running" in statuses
    assert "success" in statuses
