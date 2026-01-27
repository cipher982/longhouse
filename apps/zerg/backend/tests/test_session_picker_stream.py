"""Tests for session picker SSE events in live Oikos streams."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import pytest

from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import Run
from zerg.routers.oikos_sse import stream_run_events
from zerg.services.oikos_context import reset_oikos_context
from zerg.services.oikos_context import set_oikos_context
from zerg.services.oikos_service import OikosService
from zerg.tools.builtin.oikos_tools import request_session_selection_async


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_request_session_selection_streams_show_session_picker(db_session, test_user):
    """Ensure show_session_picker is delivered on live Oikos streams."""
    service = OikosService(db_session)
    fiche = service.get_or_create_oikos_fiche(test_user.id)
    thread = service.get_or_create_oikos_thread(test_user.id, fiche)

    run = Run(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    connected = asyncio.Event()
    events: list[dict] = []

    async def consume_stream() -> None:
        async for evt in stream_run_events(run.id, test_user.id):
            events.append(evt)
            if evt.get("event") == "connected":
                connected.set()
            if evt.get("event") == "show_session_picker":
                break

    consumer_task = asyncio.create_task(consume_stream())
    token = set_oikos_context(
        run_id=run.id,
        owner_id=test_user.id,
        message_id="test-message-id",
        trace_id="trace-test-123",
    )

    try:
        await asyncio.wait_for(connected.wait(), timeout=2)
        result = await request_session_selection_async(query="resume", project="zerg")
        assert "Session picker opened" in result
        await asyncio.wait_for(consumer_task, timeout=5)
    finally:
        reset_oikos_context(token)
        if not consumer_task.done():
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task

    picker_events = [evt for evt in events if evt.get("event") == "show_session_picker"]
    assert picker_events, "Expected show_session_picker event to be streamed"

    data = json.loads(picker_events[0]["data"])
    assert data["type"] == "show_session_picker"
    payload = data["payload"]
    assert payload["run_id"] == run.id
    assert payload["trace_id"] == "trace-test-123"
    assert payload["filters"]["project"] == "zerg"
    assert payload["filters"]["query"] == "resume"
