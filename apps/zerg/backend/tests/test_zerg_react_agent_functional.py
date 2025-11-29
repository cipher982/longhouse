"""
Functional tests for the LangGraph ReAct agent definition in zerg_react_agent.
"""

import importlib
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage

import zerg.agents_def.zerg_react_agent as agent_mod


def test_get_current_time_returns_iso8601():
    # Ensure the get_current_time tool returns a valid ISO-8601 string
    # The tool is a StructuredTool: use .invoke with empty args
    ts = agent_mod.get_current_time.invoke({})
    # Should parse without error
    parsed = datetime.fromisoformat(ts)
    assert isinstance(parsed, datetime)


@pytest.fixture()
def dummy_agent_row():
    """Return a minimal pseudo-ORM row compatible with get_runnable."""

    return SimpleNamespace(model="dummy", system_instructions="sys")


# The runnable produced by get_runnable is synchronous when tools are stubbed
# and MemorySaver is disabled, so a plain function test is sufficient.
def test_basic_llm_invoke(monkeypatch, dummy_agent_row):
    """
    Verify that the ReAct graph appends a single AI response when no tools are called.
    """
    # Restore real StateGraph and functional decorators
    import langgraph.func
    import langgraph.graph
    import langgraph.graph.message

    importlib.reload(langgraph.graph)
    importlib.reload(langgraph.func)
    importlib.reload(langgraph.graph.message)
    # Reload agent module to pick up fresh graph class
    mod = importlib.reload(importlib.import_module("zerg.agents_def.zerg_react_agent"))

    # Stub ChatOpenAI to return a fixed AIMessage
    class DummyLLM:
        def __init__(self, model, temperature=None, streaming=False, api_key=None, **kwargs):
            pass

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(content="res")

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content="res")

    # Patch LLM and disable MemorySaver to avoid checkpoint config errors
    monkeypatch.setattr(mod, "ChatOpenAI", DummyLLM)
    monkeypatch.setattr(mod, "MemorySaver", lambda *a, **kw: None)
    # Ensure token streaming flag is disabled so callbacks kwarg isn't added
    monkeypatch.setenv("LLM_TOKEN_STREAM", "false")
    # Build the runnable and invoke with one HumanMessage
    runnable = mod.get_runnable(dummy_agent_row)
    user = HumanMessage(content="hi")
    out = runnable.invoke([user])
    # The runnable returns a list of messages; last one should be our AI response
    assert isinstance(out, list)
    assert out[-1].content == "res"


def test_get_tool_messages_no_calls():
    """
    Ensure get_tool_messages returns empty when AIMessage has no tool_calls.
    """
    from langchain_core.messages import AIMessage

    from zerg.agents_def.zerg_react_agent import get_tool_messages

    msg = AIMessage(content="foo")
    result = get_tool_messages(msg)
    assert isinstance(result, list)
    assert result == []


def test_get_tool_messages_with_call(monkeypatch):
    """
    Ensure get_tool_messages invokes tools correctly and returns ToolMessage.
    """
    from langchain_core.messages import AIMessage
    from langchain_core.messages import ToolMessage

    import zerg.agents_def.zerg_react_agent as mod
    from zerg.agents_def.zerg_react_agent import get_tool_messages

    # Replace the get_current_time tool with a dummy implementation
    DummyTool = type(
        "DummyTool",
        (),
        {"name": mod.get_current_time.name, "invoke": staticmethod(lambda args: "NOW")},
    )
    monkeypatch.setattr(mod, "get_current_time", DummyTool)

    # Create an AIMessage with one tool call referencing our DummyTool
    tc = {"name": DummyTool.name, "args": {}, "id": "abc"}
    msg = AIMessage(content="", tool_calls=[tc])  # type: ignore[arg-type]
    result = get_tool_messages(msg)
    # Should be one ToolMessage with matching fields
    assert isinstance(result, list) and len(result) == 1
    tmsg = result[0]
    assert isinstance(tmsg, ToolMessage)
    assert tmsg.content == "NOW"
    assert tmsg.name == DummyTool.name
    assert tmsg.tool_call_id == "abc"
