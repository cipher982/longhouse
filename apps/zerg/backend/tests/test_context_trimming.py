"""Tests for deterministic context trimming in supervisor loop."""

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage

from zerg.services.supervisor_react_engine import _trim_messages_for_context


def test_trim_by_user_turns_keeps_last_turn():
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="u1"),
        AIMessage(content="a1"),
        ToolMessage(content="t1", tool_call_id="tc1", name="tool"),
        AIMessage(content="a1b"),
        HumanMessage(content="u2"),
        AIMessage(content="a2"),
    ]

    trimmed = _trim_messages_for_context(messages, max_user_turns=1, max_chars=0)

    assert len(trimmed) == 3
    assert isinstance(trimmed[0], SystemMessage)
    assert isinstance(trimmed[1], HumanMessage)
    assert trimmed[1].content == "u2"
    assert isinstance(trimmed[2], AIMessage)
    assert trimmed[2].content == "a2"


def test_trim_by_char_budget_drops_oldest_segment():
    messages = [
        SystemMessage(content="S" * 5),
        HumanMessage(content="U" * 20),
        AIMessage(content="A" * 20),
        HumanMessage(content="U" * 10),
        AIMessage(content="A" * 10),
    ]

    max_chars = 5 + 10 + 10 + 1
    trimmed = _trim_messages_for_context(messages, max_user_turns=0, max_chars=max_chars)

    assert len(trimmed) == 3
    assert isinstance(trimmed[0], SystemMessage)
    assert isinstance(trimmed[1], HumanMessage)
    assert isinstance(trimmed[2], AIMessage)
    assert trimmed[1].content == "U" * 10
    assert trimmed[2].content == "A" * 10


def test_no_trim_when_limits_disabled():
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="u1"),
        AIMessage(content="a1"),
    ]

    trimmed = _trim_messages_for_context(messages, max_user_turns=0, max_chars=0)

    assert trimmed is messages
