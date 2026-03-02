"""Deterministic dispatch-contract checks for scripted Oikos behavior."""

from zerg.testing.scripted_llm import ScriptedChatLLM
from zerg.types.messages import HumanMessage
from zerg.types.messages import SystemMessage


def _oikos_messages(user_prompt: str):
    return [
        SystemMessage(content="You are Oikos, a personal AI assistant."),
        HumanMessage(content=user_prompt),
    ]


def test_dispatch_direct_response_no_tool_call():
    """Simple prompts should resolve directly with no tool dispatch."""
    llm = ScriptedChatLLM()
    response = llm._generate_native(_oikos_messages("what is 2 + 2?"))

    assert response.tool_calls == []
    assert response.content == "4"


def test_dispatch_quick_tool_memory_search():
    """Quick utility tasks should dispatch a direct tool call (not commis)."""
    llm = ScriptedChatLLM()
    response = llm._generate_native(_oikos_messages("MEMORY_E2E_SEARCH: deployment notes"))

    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["name"] == "search_memory"


def test_dispatch_commis_delegation_for_infra_task():
    """Infrastructure investigations should delegate to commis."""
    llm = ScriptedChatLLM()
    response = llm._generate_native(_oikos_messages("Check disk space on cube and summarize."))

    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["name"] == "spawn_commis"
