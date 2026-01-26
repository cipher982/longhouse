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
        scenario = find_matching_scenario("check disk space on cube", "concierge")
        assert scenario is not None
        assert scenario.get("evidence_keyword") == "45%"

    def test_matches_disk_space_variations(self):
        prompts = [
            "Check disk space on cube server",
            "what is the disk space on cube?",
            "show me storage on cube",
        ]
        for prompt in prompts:
            scenario = find_matching_scenario(prompt, "concierge")
            assert scenario is not None, f"Failed to match: {prompt}"

    def test_matches_parallel_disk_space(self):
        scenario = find_matching_scenario("check disk space on cube, clifford, and zerg", "concierge")
        assert scenario is not None
        assert scenario.get("name") == "disk_space_parallel_concierge"

    def test_matches_parallel_disk_space_without_cube(self):
        scenario = find_matching_scenario("check disk space on clifford and zerg", "concierge")
        assert scenario is not None
        assert scenario.get("name") == "disk_space_parallel_concierge"

    def test_commis_matches_disk_task(self):
        scenario = find_matching_scenario(
            "Check disk space on cube server using df -h command", "commis"
        )
        assert scenario is not None
        assert scenario.get("role") == "commis"

    def test_concierge_fallback(self):
        scenario = find_matching_scenario("do something random", "concierge")
        assert scenario is not None
        # Should get the generic fallback

    def test_no_match_for_commis_random(self):
        scenario = find_matching_scenario("do something completely unrelated", "commis")
        assert scenario is None


class TestDetectRoleFromMessages:
    """Test role detection logic."""

    def test_short_system_prompt_is_commis(self):
        messages = [
            SystemMessage(content="You are a commis. Execute this task."),
            HumanMessage(content="Check disk space on cube"),
        ]
        assert detect_role_from_messages(messages) == "commis"

    def test_long_system_prompt_is_concierge(self):
        messages = [
            SystemMessage(content="You are Jarvis. " + "x" * 2000),  # Long prompt
            HumanMessage(content="Hello"),
        ]
        assert detect_role_from_messages(messages) == "concierge"

    def test_spawn_commis_call_indicates_concierge(self):
        messages = [
            HumanMessage(content="Check disk"),
            AIMessage(content="", tool_calls=[{"id": "call_123", "name": "spawn_commis", "args": {}}]),
        ]
        assert detect_role_from_messages(messages) == "concierge"


class TestScriptedChatLLM:
    """Test the ScriptedChatLLM class."""

    def test_concierge_emits_spawn_commis(self):
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
        assert ai_msg.tool_calls[0]["name"] == "spawn_commis"

    def test_concierge_emits_parallel_spawn_commis(self):
        llm = ScriptedChatLLM()
        llm = llm.bind_tools([])  # Bind empty tools

        messages = [
            SystemMessage(content="You are Jarvis. " + "x" * 2000),
            HumanMessage(content="check disk space on cube, clifford, and zerg in parallel"),
        ]

        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert isinstance(ai_msg, AIMessage)
        assert ai_msg.tool_calls
        assert len(ai_msg.tool_calls) == 3
        assert all(call["name"] == "spawn_commis" for call in ai_msg.tool_calls)

    def test_commis_emits_ssh_exec(self):
        llm = ScriptedChatLLM()
        llm = llm.bind_tools([])

        messages = [
            SystemMessage(content="Execute task."),  # Short = commis
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

        # Concierge messages with tool result
        messages = [
            SystemMessage(content="You are Jarvis. " + "x" * 2000),
            HumanMessage(content="check disk space on cube"),
            AIMessage(content="", tool_calls=[{"id": "call_123", "name": "spawn_commis", "args": {}}]),
            ToolMessage(content="Commis completed. /dev/sda1 45%", tool_call_id="call_123"),
        ]

        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert isinstance(ai_msg, AIMessage)
        assert not ai_msg.tool_calls  # No more tool calls
        assert "45%" in ai_msg.content  # Evidence keyword present

    def test_final_response_injects_keyword_when_missing(self):
        llm = ScriptedChatLLM()
        llm = llm.bind_tools([])

        messages = [
            SystemMessage(content="You are Jarvis. " + "x" * 2000),
            HumanMessage(content="check disk space on cube"),
            AIMessage(content="", tool_calls=[{"id": "call_123", "name": "spawn_commis", "args": {}}]),
            ToolMessage(content="Commis completed.", tool_call_id="call_123"),
        ]

        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert isinstance(ai_msg, AIMessage)
        assert not ai_msg.tool_calls
        assert "45%" in ai_msg.content

    def test_bind_tools_returns_new_instance(self):
        llm1 = ScriptedChatLLM()
        llm2 = llm1.bind_tools(["tool1", "tool2"])

        assert llm2 is not llm1
        assert llm2._tools == ["tool1", "tool2"]
        assert llm1._tools == []


class TestGetScenarioEvidenceKeyword:
    """Test the evidence keyword helper function."""

    def test_returns_keyword_for_disk_check(self):
        keyword = get_scenario_evidence_keyword("check disk space on cube", "concierge")
        assert keyword == "45%"

    def test_returns_none_for_generic(self):
        keyword = get_scenario_evidence_keyword("random request", "concierge")
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


class TestSequencedResponses:
    """Test sequenced response functionality for concierge replay simulation."""

    def test_sequenced_response_on_first_call(self):
        """Test that sequenced response is returned on first matching call."""
        llm = ScriptedChatLLM(
            sequences=[
                {
                    "prompt_pattern": "disk",
                    "call_number": 0,
                    "response": AIMessage(
                        content="",
                        tool_calls=[{"id": "seq-call-1", "name": "spawn_commis", "args": {"task": "Check disk space"}}],
                    ),
                },
            ]
        )

        messages = [HumanMessage(content="check disk usage")]
        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert ai_msg.tool_calls[0]["id"] == "seq-call-1"
        assert ai_msg.tool_calls[0]["args"]["task"] == "Check disk space"

    def test_sequenced_response_returns_different_on_replay(self):
        """Test that different response is returned on second call (replay simulation)."""
        llm = ScriptedChatLLM(
            sequences=[
                {
                    "prompt_pattern": "disk",
                    "call_number": 0,
                    "response": AIMessage(
                        content="",
                        tool_calls=[{"id": "call-first", "name": "spawn_commis", "args": {"task": "Check disk space"}}],
                    ),
                },
                {
                    "prompt_pattern": "disk",
                    "call_number": 1,
                    "response": AIMessage(
                        content="",
                        tool_calls=[{"id": "call-second", "name": "spawn_commis", "args": {"task": "Check disk usage"}}],
                    ),
                },
            ]
        )

        messages = [HumanMessage(content="check disk on server")]

        # First call
        result1 = llm._generate(messages)
        ai_msg1 = result1.generations[0].message
        assert ai_msg1.tool_calls[0]["id"] == "call-first"
        assert ai_msg1.tool_calls[0]["args"]["task"] == "Check disk space"

        # Second call (replay) - should get different response
        result2 = llm._generate(messages)
        ai_msg2 = result2.generations[0].message
        assert ai_msg2.tool_calls[0]["id"] == "call-second"
        assert ai_msg2.tool_calls[0]["args"]["task"] == "Check disk usage"

    def test_falls_back_to_default_when_no_sequence_match(self):
        """Test fallback to default behavior when no sequence matches."""
        llm = ScriptedChatLLM(
            sequences=[
                {
                    "prompt_pattern": "specific_keyword",  # Won't match
                    "call_number": 0,
                    "response": AIMessage(content="sequenced response"),
                },
            ]
        )
        llm = llm.bind_tools([])

        messages = [
            SystemMessage(content="Short system prompt"),
            HumanMessage(content="random request"),  # Doesn't contain "specific_keyword"
        ]

        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        # Should fall through to default behavior (generic "ok" response)
        assert ai_msg.content == "ok"

    def test_reset_call_counts(self):
        """Test that call counts can be reset for fresh test runs."""
        llm = ScriptedChatLLM(
            sequences=[
                {
                    "prompt_pattern": "disk",
                    "call_number": 0,
                    "response": AIMessage(content="first"),
                },
                {
                    "prompt_pattern": "disk",
                    "call_number": 1,
                    "response": AIMessage(content="second"),
                },
            ]
        )

        messages = [HumanMessage(content="check disk")]

        # First call
        result1 = llm._generate(messages)
        assert result1.generations[0].message.content == "first"

        # Second call
        result2 = llm._generate(messages)
        assert result2.generations[0].message.content == "second"

        # Reset
        llm.reset_call_counts()

        # Should start from first sequence again
        result3 = llm._generate(messages)
        assert result3.generations[0].message.content == "first"

    def test_bind_tools_preserves_sequences(self):
        """Test that bind_tools preserves sequence configuration."""
        llm = ScriptedChatLLM(
            sequences=[
                {
                    "prompt_pattern": "test",
                    "call_number": 0,
                    "response": AIMessage(content="sequenced"),
                },
            ]
        )

        bound = llm.bind_tools(["tool1", "tool2"])

        messages = [HumanMessage(content="test message")]
        result = bound._generate(messages)

        assert result.generations[0].message.content == "sequenced"

    def test_dict_response_format(self):
        """Test that dict format responses are converted to AIMessage."""
        llm = ScriptedChatLLM(
            sequences=[
                {
                    "prompt_pattern": "test",
                    "call_number": 0,
                    "response": {
                        "content": "dict response",
                        "tool_calls": [{"id": "dict-call", "name": "some_tool", "args": {}}],
                    },
                },
            ]
        )

        messages = [HumanMessage(content="test message")]
        result = llm._generate(messages)
        ai_msg = result.generations[0].message

        assert isinstance(ai_msg, AIMessage)
        assert ai_msg.content == "dict response"
        assert ai_msg.tool_calls[0]["id"] == "dict-call"
