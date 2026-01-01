"""Unit tests for the scripted LLM implementation."""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage

from zerg.testing.scripted_llm import (
    ScriptedChatLLM,
    detect_role_from_messages,
    find_matching_scenario,
    get_scenario_evidence_keyword,
)


class TestFindMatchingScenario:
    """Test scenario matching logic."""

    def test_matches_disk_space_check(self):
        scenario = find_matching_scenario("check disk space on cube", "supervisor")
        assert scenario is not None
        assert scenario.get("evidence_keyword") == "45%"

    def test_matches_disk_space_variations(self):
        prompts = [
            "Check disk space on cube server",
            "what is the disk space on cube?",
            "show me storage on cube",
        ]
        for prompt in prompts:
            scenario = find_matching_scenario(prompt, "supervisor")
            assert scenario is not None, f"Failed to match: {prompt}"

    def test_worker_matches_disk_task(self):
        scenario = find_matching_scenario(
            "Check disk space on cube server using df -h command", "worker"
        )
        assert scenario is not None
        assert scenario.get("role") == "worker"

    def test_supervisor_fallback(self):
        scenario = find_matching_scenario("do something random", "supervisor")
        assert scenario is not None
        # Should get the generic fallback

    def test_no_match_for_worker_random(self):
        scenario = find_matching_scenario("do something completely unrelated", "worker")
        assert scenario is None


class TestDetectRoleFromMessages:
    """Test role detection logic."""

    def test_short_system_prompt_is_worker(self):
        messages = [
            SystemMessage(content="You are a worker. Execute this task."),
            HumanMessage(content="Check disk space on cube"),
        ]
        assert detect_role_from_messages(messages) == "worker"

    def test_long_system_prompt_is_supervisor(self):
        messages = [
            SystemMessage(content="You are Jarvis. " + "x" * 2000),  # Long prompt
            HumanMessage(content="Hello"),
        ]
        assert detect_role_from_messages(messages) == "supervisor"

    def test_spawn_worker_call_indicates_supervisor(self):
        messages = [
            HumanMessage(content="Check disk"),
            AIMessage(content="", tool_calls=[{"id": "call_123", "name": "spawn_worker", "args": {}}]),
        ]
        assert detect_role_from_messages(messages) == "supervisor"


class TestScriptedChatLLM:
    """Test the ScriptedChatLLM class."""

    def test_supervisor_emits_spawn_worker(self):
        llm = ScriptedChatLLM()
        llm = llm.bind_tools([])  # Bind empty tools

        messages = [
            SystemMessage(content="You are Jarvis. " + "x" * 2000),
            HumanMessage(content="check disk space on cube"),
        ]

        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert isinstance(ai_msg, AIMessage)
        assert ai_msg.tool_calls
        assert ai_msg.tool_calls[0]["name"] == "spawn_worker"

    def test_worker_emits_ssh_exec(self):
        llm = ScriptedChatLLM()
        llm = llm.bind_tools([])

        messages = [
            SystemMessage(content="Execute task."),  # Short = worker
            HumanMessage(content="Check disk space on cube server using df -h command"),
        ]

        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert isinstance(ai_msg, AIMessage)
        assert ai_msg.tool_calls
        assert ai_msg.tool_calls[0]["name"] == "get_current_time"

    def test_final_response_after_tool_results(self):
        llm = ScriptedChatLLM()
        llm = llm.bind_tools([])

        # Supervisor messages with tool result
        messages = [
            SystemMessage(content="You are Jarvis. " + "x" * 2000),
            HumanMessage(content="check disk space on cube"),
            AIMessage(content="", tool_calls=[{"id": "call_123", "name": "spawn_worker", "args": {}}]),
            ToolMessage(content="Worker completed. /dev/sda1 45%", tool_call_id="call_123"),
        ]

        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert isinstance(ai_msg, AIMessage)
        assert not ai_msg.tool_calls  # No more tool calls
        assert "45%" in ai_msg.content  # Evidence keyword present

    def test_bind_tools_returns_new_instance(self):
        llm1 = ScriptedChatLLM()
        llm2 = llm1.bind_tools(["tool1", "tool2"])

        assert llm2 is not llm1
        assert llm2._tools == ["tool1", "tool2"]
        assert llm1._tools == []


class TestGetScenarioEvidenceKeyword:
    """Test the evidence keyword helper function."""

    def test_returns_keyword_for_disk_check(self):
        keyword = get_scenario_evidence_keyword("check disk space on cube", "supervisor")
        assert keyword == "45%"

    def test_returns_none_for_generic(self):
        keyword = get_scenario_evidence_keyword("random request", "supervisor")
        assert keyword is None


@pytest.mark.asyncio
async def test_async_generate():
    """Test that async generation works."""
    llm = ScriptedChatLLM()
    llm = llm.bind_tools([])

    messages = [
        SystemMessage(content="You are Jarvis. " + "x" * 2000),
        HumanMessage(content="check disk space on cube"),
    ]

    result = await llm._agenerate(messages)
    ai_msg = result.generations[0].message

    assert isinstance(ai_msg, AIMessage)
    assert ai_msg.tool_calls
