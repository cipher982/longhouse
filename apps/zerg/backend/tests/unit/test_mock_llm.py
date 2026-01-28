"""Unit tests for the mock LLM implementation."""

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage

from zerg.testing.mock_llm import MockChatLLM


def test_mock_llm_tool_error_not_success():
    """MockChatLLM must not report success when tool result is an error."""
    llm = MockChatLLM()

    messages = [
        HumanMessage(content="anything"),
        ToolMessage(
            content="Error: Cannot spawn commis - no credential context available",
            tool_call_id="call_123",
        ),
    ]

    result = llm._generate(messages)
    ai_msg = result.generations[0].message

    assert isinstance(ai_msg, AIMessage)
    content_lower = ai_msg.content.lower()
    assert "completed successfully" not in content_lower
    assert "error" in content_lower or "failed" in content_lower
