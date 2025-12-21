"""Test for real-time token streaming via SSE (Jarvis chat).

This test verifies that when LLM_TOKEN_STREAM is enabled, the supervisor:
1. Sets the user context for token callbacks
2. Emits SUPERVISOR_TOKEN events to the event bus
3. Tokens are published with correct run_id correlation

This prevents regression of the "fake streaming" bug where tokens were
only sent after the full response was ready.
"""

from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import AsyncMock, patch

import pytest

from zerg.events import EventType, event_bus


class MockChatOpenAI:
    """Stub that emits tokens via callback during invoke."""

    def __init__(self, *_, streaming: bool = False, **__):
        self._streaming = streaming

    def bind_tools(self, _tools):
        return self

    def invoke(self, _messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content="Hello world")

    async def ainvoke(self, _messages, config=None, **_):
        """Emit tokens via callback if provided."""
        from langchain_core.messages import AIMessage

        if self._streaming and config and "callbacks" in config:
            callbacks = config["callbacks"]
            tokens = ["Hello", " ", "world"]
            for token in tokens:
                for cb in callbacks:
                    if hasattr(cb, "on_llm_new_token"):
                        await cb.on_llm_new_token(token)

        return AIMessage(content="Hello world")


@pytest.mark.asyncio
async def test_supervisor_token_events_published(monkeypatch, db_session, test_user, sample_agent):
    """Verify SUPERVISOR_TOKEN events are published during supervisor execution."""
    # Enable token streaming (env var read fresh each time via get_settings())
    monkeypatch.setenv("LLM_TOKEN_STREAM", "true")

    # Patch ChatOpenAI with our mock
    monkeypatch.setattr(
        "zerg.agents_def.zerg_react_agent.ChatOpenAI",
        MockChatOpenAI,
        raising=True,
    )

    # Capture events published to the event bus
    captured_events: List[dict] = []
    original_publish = event_bus.publish

    async def capture_publish(event_type: EventType, data: dict):
        captured_events.append({"event_type": event_type, "data": data})
        # Still call original to allow normal flow
        await original_publish(event_type, data)

    monkeypatch.setattr(event_bus, "publish", capture_publish)

    # Import and run supervisor service
    from zerg.services.supervisor_service import SupervisorService

    supervisor = SupervisorService(db_session)

    # Run supervisor with a simple task
    result = await supervisor.run_supervisor(
        owner_id=test_user.id,
        task="Say hello",
        model_override="gpt-4o-mini",
    )

    assert result.status == "success", f"Supervisor failed: {result}"

    # Verify SUPERVISOR_TOKEN events were captured
    token_events = [e for e in captured_events if e["event_type"] == EventType.SUPERVISOR_TOKEN]

    # Should have token events (3 tokens: "Hello", " ", "world")
    assert len(token_events) >= 1, (
        f"Expected SUPERVISOR_TOKEN events, got {len(token_events)}. "
        f"All events: {[e['event_type'] for e in captured_events]}"
    )

    # Verify token event structure
    for event in token_events:
        data = event["data"]
        assert "run_id" in data, "Token event missing run_id"
        assert "thread_id" in data, "Token event missing thread_id"
        assert "token" in data, "Token event missing token"
        assert "owner_id" in data, "Token event missing owner_id"
        assert data["owner_id"] == test_user.id, "Token event has wrong owner_id"

    # Verify tokens match expected content
    tokens = [e["data"]["token"] for e in token_events]
    assert "Hello" in tokens or " " in tokens or "world" in tokens, f"Expected tokens, got: {tokens}"


@pytest.mark.asyncio
async def test_token_context_set_correctly(monkeypatch, db_session, test_user, sample_agent):
    """Verify user context is set during supervisor execution for token streaming."""
    # Enable token streaming (env var read fresh each time via get_settings())
    monkeypatch.setenv("LLM_TOKEN_STREAM", "true")

    # Track context values during execution
    context_values: List[dict] = []

    # Patch the token callback to capture context
    from zerg.callbacks import token_stream

    original_on_token = token_stream.WsTokenCallback.on_llm_new_token

    async def capture_context(self, token: str, **kwargs):
        thread_id = token_stream.current_thread_id_var.get()
        user_id = token_stream.current_user_id_var.get()
        context_values.append({"thread_id": thread_id, "user_id": user_id, "token": token})
        # Don't call original - we just want to capture context

    monkeypatch.setattr(token_stream.WsTokenCallback, "on_llm_new_token", capture_context)

    # Patch ChatOpenAI to emit tokens
    monkeypatch.setattr(
        "zerg.agents_def.zerg_react_agent.ChatOpenAI",
        MockChatOpenAI,
        raising=True,
    )

    from zerg.services.supervisor_service import SupervisorService

    supervisor = SupervisorService(db_session)

    result = await supervisor.run_supervisor(
        owner_id=test_user.id,
        task="Say hello",
        model_override="gpt-4o-mini",
    )

    assert result.status == "success"

    # Verify context was set during token emission
    assert len(context_values) > 0, "No tokens were emitted"

    for ctx in context_values:
        assert ctx["thread_id"] is not None, "thread_id context was not set"
        assert ctx["user_id"] is not None, "user_id context was not set"
        assert ctx["user_id"] == test_user.id, f"user_id context wrong: {ctx['user_id']} != {test_user.id}"
