"""Scripted LLM implementation for deterministic unit tests.

This module provides a lightweight, scenario-driven chat model used by tests to
exercise supervisor/worker plumbing without calling real LLM APIs.

It is intentionally minimal: only the behaviors required by the unit tests are
implemented.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult


def detect_role_from_messages(messages: List[BaseMessage]) -> str:
    """Best-effort role detector used by scripted scenarios."""
    # If we see a spawn_worker call, assume supervisor.
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for call in msg.tool_calls:
                if call.get("name") == "spawn_worker":
                    return "supervisor"

    # Otherwise, infer from system prompt length (tests use this heuristic).
    first_system = next((m for m in messages if isinstance(m, SystemMessage)), None)
    if first_system and len(str(first_system.content)) > 1000:
        return "supervisor"

    return "worker"


def find_matching_scenario(prompt: str, role: str) -> Optional[Dict[str, Any]]:
    """Return a scenario dict for (prompt, role), or None if no match."""
    text = (prompt or "").lower()
    role = (role or "").lower()

    is_disk = any(k in text for k in ("disk", "storage", "space"))
    is_cube = "cube" in text
    is_clifford = "clifford" in text
    is_zerg = "zerg" in text
    host_count = sum([is_cube, is_clifford, is_zerg])
    # Heuristic: multi-host disk checks imply parallel intent even without the keyword.
    is_parallel = "parallel" in text or host_count >= 2

    if role == "worker":
        if is_disk and (is_cube or is_clifford or is_zerg):
            return {
                "role": "worker",
                "name": "disk_space_worker",
                "evidence_keyword": "45%",
            }
        return None

    # Supervisor: parallel disk checks first, then single-host disk checks.
    if is_disk and is_parallel:
        return {
            "role": "supervisor",
            "name": "disk_space_parallel_supervisor",
            "evidence_keyword": "45%",
        }

    # Supervisor: match disk check and provide a generic fallback for everything else.
    if is_disk and is_cube:
        return {
            "role": "supervisor",
            "name": "disk_space_supervisor",
            "evidence_keyword": "45%",
        }

    return {
        "role": "supervisor",
        "name": "generic_fallback",
        "evidence_keyword": None,
    }


def get_scenario_evidence_keyword(prompt: str, role: str) -> str | None:
    scenario = find_matching_scenario(prompt, role)
    if not scenario:
        return None
    keyword = scenario.get("evidence_keyword")
    return keyword if isinstance(keyword, str) else None


class ScriptedChatLLM(BaseChatModel):
    """A deterministic chat model driven by simple prompt scenarios.

    Supports both static scenarios (default behavior) and sequenced responses
    for testing supervisor replay behavior where the LLM might produce different
    outputs on subsequent calls.

    Usage with sequences:
        llm = ScriptedChatLLM(sequences=[
            {
                "prompt_pattern": "disk",
                "call_number": 0,
                "response": AIMessage(content="", tool_calls=[...])
            },
            {
                "prompt_pattern": "disk",
                "call_number": 1,
                "response": AIMessage(content="Rephrased response")
            },
        ])
    """

    model_name: str = "gpt-scripted"

    def __init__(self, sequences: List[Dict[str, Any]] | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._tools: list[Any] = []
        self._sequences: List[Dict[str, Any]] = sequences or []
        self._call_counts: Dict[str, int] = {}

    def bind_tools(self, tools):  # noqa: ANN001 - signature mirrors LangChain
        bound = ScriptedChatLLM(sequences=self._sequences)
        bound._tools = list(tools)
        bound._call_counts = self._call_counts  # Share call counts across bindings
        return bound

    def _get_sequenced_response(self, prompt: str) -> Optional[AIMessage]:
        """Return a sequenced response if one matches the prompt and call count.

        This enables testing replay behavior where the LLM might
        return different responses on subsequent calls (e.g., slightly
        rephrased task descriptions).

        Args:
            prompt: The prompt text to match against

        Returns:
            AIMessage if a sequence matches, None to fall back to default behavior
        """
        if not self._sequences:
            return None

        prompt_lower = prompt.lower()

        # Find matching sequences
        for seq in self._sequences:
            pattern = seq.get("prompt_pattern", "")
            if not pattern or pattern.lower() not in prompt_lower:
                continue

            # Track call count per pattern
            call_count = self._call_counts.get(pattern, 0)
            expected_call = seq.get("call_number", 0)

            if call_count == expected_call:
                # Increment call count for this pattern
                self._call_counts[pattern] = call_count + 1
                response = seq.get("response")
                if isinstance(response, AIMessage):
                    return response
                elif isinstance(response, dict):
                    # Allow dict format for convenience
                    return AIMessage(
                        content=response.get("content", ""),
                        tool_calls=response.get("tool_calls", []),
                    )

        return None

    def reset_call_counts(self):
        """Reset call counts for a fresh test run."""
        self._call_counts.clear()

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,  # noqa: ARG002 - unused (deterministic)
        run_manager: Optional[CallbackManagerForLLMRun] = None,  # noqa: ARG002 - unused
        **kwargs: Any,  # noqa: ARG002 - unused
    ) -> ChatResult:
        role = detect_role_from_messages(messages)
        last_user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        prompt = str(last_user.content) if last_user else ""

        # Check for sequenced response first (enables replay behavior testing)
        sequenced_response = self._get_sequenced_response(prompt)
        if sequenced_response is not None:
            return ChatResult(generations=[ChatGeneration(message=sequenced_response)])

        # If tool results are present, emit final synthesis (no more tool calls).
        tool_msg = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
        if tool_msg is not None:
            content = str(tool_msg.content)
            keyword = "45%" if "45%" in content else None
            if not keyword:
                scenario = find_matching_scenario(prompt, role)
                scenario_keyword = scenario.get("evidence_keyword") if scenario else None
                if isinstance(scenario_keyword, str) and scenario_keyword:
                    keyword = scenario_keyword
            final_text = (
                f"Cube is at {keyword} disk usage; biggest usage is Docker images/volumes." if keyword else "Task completed successfully."
            )
            ai_message = AIMessage(content=final_text, tool_calls=[])
            return ChatResult(generations=[ChatGeneration(message=ai_message)])

        scenario = find_matching_scenario(prompt, role)

        if role == "supervisor" and scenario and scenario.get("name") == "disk_space_parallel_supervisor":
            tasks = [
                "Check disk space on cube and identify what is using space",
                "Check disk space on clifford and identify what is using space",
                "Check disk space on zerg and identify what is using space",
            ]
            tool_calls = [
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": "spawn_worker",
                    "args": {"task": task},
                }
                for task in tasks
            ]
            ai_message = AIMessage(content="", tool_calls=tool_calls)
            return ChatResult(generations=[ChatGeneration(message=ai_message)])

        if role == "supervisor" and scenario and scenario.get("name") == "disk_space_supervisor":
            tool_call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": "spawn_worker",
                "args": {"task": "Check disk space on cube and identify what is using space"},
            }
            ai_message = AIMessage(content="", tool_calls=[tool_call])
            return ChatResult(generations=[ChatGeneration(message=ai_message)])

        if role == "worker" and scenario and scenario.get("name") == "disk_space_worker":
            tool_call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": "get_current_time",
                "args": {},
            }
            ai_message = AIMessage(content="", tool_calls=[tool_call])
            return ChatResult(generations=[ChatGeneration(message=ai_message)])

        ai_message = AIMessage(content="ok", tool_calls=[])
        return ChatResult(generations=[ChatGeneration(message=ai_message)])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(0)  # allow scheduling; keep deterministic
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "scripted-chat"

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {"model_name": self.model_name}
