from unittest.mock import AsyncMock

import pytest

from zerg.crud import crud
from zerg.events import EventType
from zerg.events import event_bus
from zerg.services.ops_events import OpsEventsBridge
from zerg.websocket.handlers import _subscribe_ops_events
from zerg.websocket.manager import topic_manager


@pytest.mark.asyncio
async def test_ops_events_bridge_run_success_broadcast(db_session):
    # Prepare admin user and websocket
    admin = crud.create_user(db_session, email="ops-admin@local", provider=None, role="ADMIN")
    client_id = "client-ops"
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    await topic_manager.connect(client_id, ws, user_id=admin.id)
    await _subscribe_ops_events(client_id, "m1", db_session)

    # Start a fresh bridge for this test
    bridge = OpsEventsBridge()
    bridge.start()
    try:
        # Publish RUN_UPDATED with success
        await event_bus.publish(
            EventType.COURSE_UPDATED,
            {
                "event_type": "course_updated",
                "fiche_id": 123,
                "course_id": 456,
                "status": "success",
            },
        )

        # Verify an ops_event frame was sent
        ws.send_json.assert_called()
        env = ws.send_json.call_args[0][0]
        assert env["topic"] == "ops:events"
        assert env["type"] == "ops_event"
        assert env["data"]["type"] == "run_success"
    finally:
        bridge.stop()
        await topic_manager.disconnect(client_id)


@pytest.mark.asyncio
async def test_ops_events_subscription_admin_gated(db_session):
    # Non-admin user
    user = crud.create_user(db_session, email="ops-user@local", provider=None, role="USER")
    client_id = "client-nonadmin"
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    await topic_manager.connect(client_id, ws, user_id=user.id)
    await _subscribe_ops_events(client_id, "m2", db_session)

    # Should receive an error envelope and a close call
    ws.send_json.assert_called()
    sent = ws.send_json.call_args[0][0]
    assert sent["type"] == "error"
    ws.close.assert_called()


@pytest.mark.asyncio
async def test_ops_budget_denied_bridge(db_session):
    admin = crud.create_user(db_session, email="ops-admin2@local", provider=None, role="ADMIN")
    client_id = "client-ops2"
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    await topic_manager.connect(client_id, ws, user_id=admin.id)
    await _subscribe_ops_events(client_id, "m3", db_session)

    bridge = OpsEventsBridge()
    bridge.start()
    try:
        await event_bus.publish(
            EventType.BUDGET_DENIED,
            {
                "scope": "user",
                "percent": 100.0,
                "used_usd": 1.0,
                "limit_cents": 100,
                "user_email": "u@local",
            },
        )
        ws.send_json.assert_called()
        env = ws.send_json.call_args[0][0]
        assert env["topic"] == "ops:events"
        assert env["type"] == "ops_event"
        assert env["data"]["type"] == "budget_denied"
    finally:
        bridge.stop()
        await topic_manager.disconnect(client_id)
