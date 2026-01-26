"""Tests for session picker SSE events in live Jarvis streams."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import pytest

from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.routers.jarvis_sse import stream_run_events
from zerg.services.supervisor_context import reset_supervisor_context
from zerg.services.supervisor_context import set_supervisor_context
from zerg.services.supervisor_service import SupervisorService
from zerg.tools.builtin.supervisor_tools import request_session_selection_async


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_request_session_selection_streams_show_session_picker(db_session, test_user):
    """Ensure show_session_picker is delivered on live Jarvis streams."""
    service = SupervisorService(db_session)
    agent = service.get_or_create_supervisor_agent(test_user.id)
    thread = service.get_or_create_supervisor_thread(test_user.id, agent)

    run = AgentRun(
        agent_id=agent.id,
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
    token = set_supervisor_context(
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
        reset_supervisor_context(token)
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
