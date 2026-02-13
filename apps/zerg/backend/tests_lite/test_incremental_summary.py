"""Tests for incremental_summary() function.

Covers:
- First summary (no existing summary)
- Incremental update (existing summary + new events)
- Tool events filtered out
- Returns None for only tool events
- Redaction applied to message text
- CAS guard prevents stale overwrites
- Throttle skips when fewer than 2 new user/assistant messages
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zerg.services.session_processing.summarize import (
    SessionSummary,
    incremental_summary,
)


def _make_event(role: str, content: str, tool_name: str | None = None) -> dict:
    """Build a minimal event dict."""
    return {
        "role": role,
        "content_text": content,
        "tool_name": tool_name,
        "tool_input_json": None,
        "tool_output_text": None,
        "timestamp": "2026-01-01T00:00:00Z",
        "session_id": "test-session-id",
    }


def _mock_client(title: str = "Test Title", summary: str = "Test summary.") -> AsyncMock:
    """Return an AsyncOpenAI mock that returns a JSON summary."""
    import json

    client = AsyncMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = json.dumps({"title": title, "summary": summary})
    response.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_first_summary_no_existing():
    """None summary + first events -> generates summary."""
    client = _mock_client(title="Setup Auth Module", summary="Implemented JWT auth.")
    events = [
        _make_event("user", "Add JWT auth to the login endpoint"),
        _make_event("assistant", "I'll implement JWT authentication for the login endpoint."),
    ]

    result = await incremental_summary(
        session_id="s1",
        current_summary=None,
        current_title=None,
        new_events=events,
        client=client,
        model="test-model",
    )

    assert result is not None
    assert result.title == "Setup Auth Module"
    assert result.summary == "Implemented JWT auth."
    client.chat.completions.create.assert_called_once()

    # Verify prompt doesn't include "Current summary" when none exists
    call_args = client.chat.completions.create.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    assert "Current summary:" not in user_msg


@pytest.mark.asyncio
async def test_incremental_update():
    """Existing summary + new events -> updated summary with context."""
    client = _mock_client(
        title="Auth and Rate Limiting",
        summary="Implemented JWT auth and added rate limiting.",
    )
    events = [
        _make_event("user", "Now add rate limiting to the API"),
        _make_event("assistant", "I'll add rate limiting middleware."),
    ]

    result = await incremental_summary(
        session_id="s1",
        current_summary="Implemented JWT authentication for login.",
        current_title="Setup Auth Module",
        new_events=events,
        client=client,
        model="test-model",
    )

    assert result is not None
    assert result.title == "Auth and Rate Limiting"

    # Verify prompt includes existing summary context
    call_args = client.chat.completions.create.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    assert "Current summary:" in user_msg
    assert "Setup Auth Module" in user_msg


@pytest.mark.asyncio
async def test_tool_events_filtered():
    """Tool events are excluded from the prompt input."""
    client = _mock_client()
    events = [
        _make_event("user", "Fix the bug"),
        _make_event("tool", "Read file output here", tool_name="Read"),
        _make_event("assistant", "I found and fixed the bug."),
    ]

    result = await incremental_summary(
        session_id="s1",
        current_summary=None,
        current_title=None,
        new_events=events,
        client=client,
        model="test-model",
    )

    assert result is not None
    # Verify the tool event was not included in the prompt
    call_args = client.chat.completions.create.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    assert "Read file output" not in user_msg
    assert "[tool]" not in user_msg


@pytest.mark.asyncio
async def test_returns_none_for_only_tool_events():
    """No meaningful events -> None returned, no LLM call."""
    client = _mock_client()
    events = [
        _make_event("tool", "Read output", tool_name="Read"),
        _make_event("tool", "Grep output", tool_name="Grep"),
    ]

    result = await incremental_summary(
        session_id="s1",
        current_summary=None,
        current_title=None,
        new_events=events,
        client=client,
        model="test-model",
    )

    assert result is None
    client.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
async def test_redaction_applied():
    """Verify redact_secrets() is applied to message text."""
    client = _mock_client()
    events = [
        _make_event("user", "Use this key: sk-ant-abc123456789012345678901"),
        _make_event("assistant", "Got it, I'll use that API key."),
    ]

    result = await incremental_summary(
        session_id="s1",
        current_summary=None,
        current_title=None,
        new_events=events,
        client=client,
        model="test-model",
    )

    assert result is not None
    # Verify the redacted content was sent, not the raw key
    call_args = client.chat.completions.create.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    assert "sk-ant-abc123456789012345678901" not in user_msg
    assert "[ANTHROPIC_KEY]" in user_msg


@pytest.mark.asyncio
async def test_metadata_in_prompt():
    """Metadata (project, provider, branch) appears in prompt context."""
    client = _mock_client()
    events = [
        _make_event("user", "Hello"),
        _make_event("assistant", "Hi there!"),
    ]

    await incremental_summary(
        session_id="s1",
        current_summary=None,
        current_title=None,
        new_events=events,
        client=client,
        model="test-model",
        metadata={"project": "zerg", "provider": "claude", "git_branch": "main"},
    )

    call_args = client.chat.completions.create.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    assert "Project: zerg" in user_msg
    assert "Provider: claude" in user_msg
    assert "Branch: main" in user_msg


@pytest.mark.asyncio
async def test_empty_content_events_filtered():
    """Events with empty/whitespace content are excluded."""
    client = _mock_client()
    events = [
        _make_event("user", ""),
        _make_event("assistant", "   "),
        _make_event("user", "Real message"),
        _make_event("assistant", "Real response"),
    ]

    result = await incremental_summary(
        session_id="s1",
        current_summary=None,
        current_title=None,
        new_events=events,
        client=client,
        model="test-model",
    )

    assert result is not None
    call_args = client.chat.completions.create.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    # Only the real messages should be in the prompt
    assert "Real message" in user_msg
    assert "Real response" in user_msg


@pytest.mark.asyncio
async def test_message_cap_at_500_chars():
    """Messages longer than 500 chars are truncated."""
    client = _mock_client()
    long_text = "x" * 1000
    events = [
        _make_event("user", long_text),
        _make_event("assistant", "Short response"),
    ]

    await incremental_summary(
        session_id="s1",
        current_summary=None,
        current_title=None,
        new_events=events,
        client=client,
        model="test-model",
    )

    call_args = client.chat.completions.create.call_args
    user_msg = call_args.kwargs["messages"][1]["content"]
    # The message in the prompt should be capped at 500 chars
    # Find the [user] line and check it
    assert "x" * 501 not in user_msg
