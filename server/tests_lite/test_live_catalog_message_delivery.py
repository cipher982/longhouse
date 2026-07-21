from __future__ import annotations

import pytest

from zerg.services.live_control_catalog import _finish_linked_session_message


@pytest.mark.asyncio
async def test_queued_input_delivery_converges_linked_session_message():
    calls = []

    class CatalogClient:
        async def call(self, method, params, timeout_seconds):
            calls.append((method, params, timeout_seconds))
            return {"changed": True}

    await _finish_linked_session_message(
        CatalogClient(),
        receipt={
            "id": "receipt-1",
            "owner_id": 7,
            "client_request_id": "session-message-42",
        },
        delivery_status="delivered",
        error=None,
    )

    assert len(calls) == 1
    method, params, timeout = calls[0]
    assert method == "session.message.delivery.v2"
    assert timeout == 1.0
    assert params["owner_id"] == 7
    assert params["message_id"] == 42
    assert params["expected_status"] == "queued"
    assert params["delivery_status"] == "delivered"
    assert params["delivered_via"] == "live_input_queue"
    assert params["delivered_at"] is not None
