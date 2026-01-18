"""Tests for AgentRunner done() tool tracking."""

from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage

from zerg.managers.agent_runner import AgentRunner


def test_count_done_tool_calls_dedupes_tool_call_ids():
    messages = [
        AIMessage(content="call done", tool_calls=[{"id": "t1", "name": "done", "args": {}}]),
        ToolMessage(content="Done.", tool_call_id="t1", name="done"),
        ToolMessage(content="Done again", tool_call_id="t2", name="done"),
        AIMessage(content="call done again", tool_calls=[{"id": "t2", "name": "done", "args": {}}]),
    ]

    assert AgentRunner._count_done_tool_calls(messages) == 2
