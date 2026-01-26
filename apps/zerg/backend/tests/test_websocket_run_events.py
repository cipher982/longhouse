import pytest


@pytest.fixture
def ws_client(client):
    """WebSocket client fixture"""
    with client.websocket_connect("/api/ws") as websocket:
        yield websocket


@pytest.mark.asyncio
async def test_course_update_ws_events(ws_client, client, sample_fiche):
    """When a course is triggered, WebSocket subscribers receive course_update events"""
    fiche_id = sample_fiche.id
    # Subscribe to fiche topic
    subscribe_msg = {
        "type": "subscribe",
        "topics": [f"fiche:{fiche_id}"],
        "message_id": "sub-run-updates",
    }
    ws_client.send_json(subscribe_msg)

    # Expect initial fiche_state message
    init = ws_client.receive_json()
    assert init.get("type") == "fiche_state"
    assert init.get("data", {}).get("id") == fiche_id

    # Trigger a manual run via the task endpoint
    resp = client.post(f"/api/fiches/{fiche_id}/task")
    assert resp.status_code == 202

    # Collect statuses from course_update events
    statuses = set()
    # We expect queued, running, and success events
    for _ in range(5):
        msg = ws_client.receive_json()
        if msg.get("type") != "course_update":
            continue
        data = msg.get("data", {})
        assert data.get("thread_id") is not None, f"course_update missing thread_id: {data}"
        status = data.get("status")
        if status:
            statuses.add(status)
        if statuses >= {"queued", "running", "success"}:
            break

    assert "queued" in statuses
    assert "running" in statuses
    assert "success" in statuses
