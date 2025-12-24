"""Scripted LLM implementation for deterministic E2E tests.

This module provides a mock LLM that follows scripted tool call sequences
for specific prompts. It's designed to make E2E tests fully deterministic
without requiring real OpenAI API calls.

Usage:
    1. Set agent model to "gpt-scripted"
    2. Send a prompt that matches a scripted scenario
    3. The LLM will emit the exact tool calls defined in the scenario

IMPORTANT: This is TEST-ONLY infrastructure. The gpt-scripted model
should never be used in production.

Scripted scenarios are defined in SCENARIOS dict below.
"""

import asyncio
import logging
import re
import uuid
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Scripted Scenarios
# -----------------------------------------------------------------------------
# Each scenario defines:
#   - trigger: regex pattern to match user message
#   - role: "supervisor" or "worker" (determines which tool calls to emit)
#   - tool_calls: list of tool calls to emit (one turn each)
#   - final_response: text to emit after all tool calls complete
#   - evidence_keyword: keyword that MUST appear in final response (for assertions)
# -----------------------------------------------------------------------------

SCENARIOS = {
    "disk_space_check": {
        "trigger": r"(check|show|what.*(is|are)).*(disk|storage|space).*cube",
        "role": "supervisor",
        "supervisor_tool_calls": [
            {
                "name": "spawn_worker",
                "args": {
                    "task": "Check disk space on cube server using df -h command",
                    "model": "gpt-scripted",  # Worker also uses scripted model
                    "wait": True,
                    "timeout_seconds": 60,
                },
            }
        ],
        "final_response": "Based on the disk space check, the cube server shows /dev/sda1 is at 45% capacity with 55GB available out of 100GB total. The system has adequate free space.",
        "evidence_keyword": "45%",
    },
    "disk_space_worker": {
        "trigger": r"(check|disk|space|df).*cube",
        "role": "worker",
        "worker_tool_calls": [
            {
                "name": "ssh_exec",
                "args": {"host": "cube", "command": "df -h"},
            }
        ],
        # Worker final response intentionally empty to test evidence mounting
        "final_response": "",
        "evidence_keyword": None,
    },
    # Failure scenario: SSH timeout
    "disk_space_timeout": {
        "trigger": r"check.*disk.*unreachable",
        "role": "supervisor",
        "supervisor_tool_calls": [
            {
                "name": "spawn_worker",
                "args": {
                    "task": "Check disk space on unreachable-server (will timeout)",
                    "model": "gpt-scripted",
                    "wait": True,
                    "timeout_seconds": 5,
                },
            }
        ],
        "final_response": "I attempted to check the disk space but the connection timed out. The server appears to be unreachable. The worker job shows exit code 255 indicating a connection failure.",
        "evidence_keyword": "timed out",
    },
    # Generic fallback for supervisor without triggering workers
    "generic_supervisor": {
        "trigger": r".*",
        "role": "supervisor",
        "supervisor_tool_calls": [],
        "final_response": "I understand your request. This is a scripted response for testing purposes.",
        "evidence_keyword": None,
    },
}


def find_matching_scenario(message: str, role_hint: str | None = None) -> dict | None:
    """Find a scenario that matches the given message.

    Args:
        message: The user message to match
        role_hint: Optional hint about role ("supervisor" or "worker")

    Returns:
        Matching scenario dict or None
    """
    message_lower = message.lower()

    # Try specific scenarios first (not the generic fallback)
    for name, scenario in SCENARIOS.items():
        if name == "generic_supervisor":
            continue  # Skip fallback for now
        if role_hint and scenario.get("role") != role_hint:
            continue
        pattern = scenario.get("trigger", "")
        if re.search(pattern, message_lower, re.IGNORECASE):
            logger.info(f"Matched scenario: {name}")
            return scenario

    # Fall back to generic if role_hint is supervisor
    if role_hint == "supervisor":
        return SCENARIOS.get("generic_supervisor")

    return None


def detect_role_from_messages(messages: list[BaseMessage]) -> str:
    """Detect if this is a supervisor or worker context based on messages.

    Workers typically have:
    - Shorter system prompts
    - Task-focused user messages
    - No previous spawn_worker calls

    Supervisors typically have:
    - Elaborate system prompts with capabilities
    - Conversation history with user
    """
    # Check if any tool calls mention spawn_worker (supervisor capability)
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "spawn_worker":
                    return "supervisor"

    # Check system prompt length (workers have shorter prompts)
    system_msgs = [m for m in messages if getattr(m, "type", None) == "system"]
    if system_msgs:
        system_content = str(system_msgs[0].content) if system_msgs else ""
        # Workers have task-specific short prompts
        if len(system_content) < 1000:
            return "worker"

    # Default to supervisor
    return "supervisor"


class ScriptedChatLLM(BaseChatModel):
    """A scripted chat LLM that follows predefined tool call sequences for testing.

    This LLM matches incoming prompts against scenarios and emits the exact
    tool calls defined in that scenario, enabling fully deterministic E2E tests.
    """

    model_name: str = "gpt-scripted"
    _tools: list = []
    _call_count: int = 0  # Track calls within a conversation

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tools = []
        self._call_count = 0

    def bind_tools(self, tools):
        """Bind tools to the scripted LLM."""
        bound = ScriptedChatLLM()
        bound._tools = tools
        return bound

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate a scripted response based on the messages."""
        # Detect role from messages
        role = detect_role_from_messages(messages)
        logger.info(f"ScriptedLLM detected role: {role}")

        # Find the user message to match against
        user_message = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                user_message = str(msg.content)
                break

        # Check if we've already made tool calls (have tool results in messages)
        has_tool_results = any(isinstance(m, ToolMessage) for m in messages)

        # Find matching scenario
        scenario = find_matching_scenario(user_message, role)

        if not scenario:
            # No match - return generic response
            logger.warning(f"No scenario matched for: {user_message[:100]}...")
            ai_message = AIMessage(content="I understand your request. [No scenario matched]")
            return ChatResult(generations=[ChatGeneration(message=ai_message)])

        # If we have tool results, emit final response
        if has_tool_results:
            logger.info("Tool results present - emitting final response")
            final_text = scenario.get("final_response", "Task completed.")
            ai_message = AIMessage(content=final_text)
            return ChatResult(generations=[ChatGeneration(message=ai_message)])

        # Check which tool calls to emit based on role
        tool_calls_key = f"{role}_tool_calls"
        pending_calls = scenario.get(tool_calls_key, [])

        if not pending_calls:
            # No tool calls for this role - emit final response directly
            final_text = scenario.get("final_response", "Request processed.")
            ai_message = AIMessage(content=final_text)
            return ChatResult(generations=[ChatGeneration(message=ai_message)])

        # Emit the tool calls
        tool_calls = []
        for tc in pending_calls:
            tool_calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": tc["name"],
                    "args": tc["args"],
                }
            )

        logger.info(f"ScriptedLLM emitting tool calls: {[tc['name'] for tc in tool_calls]}")
        ai_message = AIMessage(content="", tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=ai_message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate a scripted response asynchronously."""
        # Small delay to simulate API call
        await asyncio.sleep(0.05)
        return self._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        """Return identifier of llm type."""
        return "scripted-chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        """Get the identifying parameters."""
        return {"model_name": self.model_name}


def get_scenario_evidence_keyword(prompt: str, role: str = "supervisor") -> str | None:
    """Get the evidence keyword that should appear in the final response.

    Use this in tests to assert the response contains expected content.

    Args:
        prompt: The user prompt that was sent
        role: The role context

    Returns:
        The keyword that should appear in the response, or None
    """
    scenario = find_matching_scenario(prompt, role)
    if scenario:
        return scenario.get("evidence_keyword")
    return None
