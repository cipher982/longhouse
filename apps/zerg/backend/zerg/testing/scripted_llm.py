"""Scripted LLM implementation for deterministic unit tests.

This module provides a lightweight, scenario-driven chat model used by tests to
exercise oikos/commis plumbing without calling real LLM APIs.

It is intentionally minimal: only the behaviors required by the unit tests are
implemented.

NOTE: This still inherits from LangChain's BaseChatModel for interface compatibility,
but uses duck-typing for message checks to work with both langchain_core.messages
and zerg.types.messages.
"""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult

# Use native types for return values
from zerg.types.messages import AIMessage


def _is_human_message(msg: Any) -> bool:
    """Check if message is a human/user message (duck-typed)."""
    return getattr(msg, "type", None) in ("human", "user")


def _is_ai_message(msg: Any) -> bool:
    """Check if message is an AI/assistant message (duck-typed)."""
    return getattr(msg, "type", None) in ("ai", "assistant")


def _is_system_message(msg: Any) -> bool:
    """Check if message is a system message (duck-typed)."""
    return getattr(msg, "type", None) == "system"


def _is_tool_message(msg: Any) -> bool:
    """Check if message is a tool message (duck-typed)."""
    return getattr(msg, "type", None) == "tool"


def detect_role_from_messages(messages: List[Any]) -> str:
    """Best-effort role detector used by scripted scenarios."""
    # If we see a spawn_commis call, assume oikos.
    for msg in messages:
        if _is_ai_message(msg) and getattr(msg, "tool_calls", None):
            for call in msg.tool_calls:
                if call.get("name") == "spawn_commis":
                    return "oikos"

    # Otherwise, infer from system prompt length (tests use this heuristic).
    first_system = next((m for m in messages if _is_system_message(m)), None)
    if first_system:
        system_text = str(first_system.content or "").lower()
        if "you are a commis" in system_text:
            return "commis"
        if "you are oikos" in system_text:
            return "oikos"
        if len(system_text) > 1000:
            return "oikos"

    return "commis"


def _parse_memory_e2e_scenario(text: str) -> Optional[Dict[str, Any]]:
    """Parse MEMORY_E2E_* prompts for deterministic tool-call testing."""
    lowered = text.strip().lower()
    if "memory_e2e_" not in lowered:
        return None

    save_match = re.search(r"memory_e2e_save\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if save_match:
        content = save_match.group(1).strip()
        if not content:
            content = "E2E memory content"
        return {"name": "memory_e2e", "action": "save", "content": content}

    search_match = re.search(r"memory_e2e_search\s*:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if search_match:
        query = search_match.group(1).strip()
        if not query:
            query = "E2E"
        return {"name": "memory_e2e", "action": "search", "query": query}

    if "memory_e2e_list" in lowered:
        return {"name": "memory_e2e", "action": "list"}

    forget_match = re.search(r"memory_e2e_forget\s*:\s*([a-f0-9-]{8,36})", lowered)
    if not forget_match:
        forget_match = re.search(r"([a-f0-9-]{8,36})", lowered)
    if forget_match:
        return {"name": "memory_e2e", "action": "forget", "memory_id": forget_match.group(1)}

    return None


def find_matching_scenario(prompt: str, role: str) -> Optional[Dict[str, Any]]:
    """Return a scenario dict for (prompt, role), or None if no match."""
    text = (prompt or "").lower()
    role = (role or "").lower()

    memory_scenario = _parse_memory_e2e_scenario(prompt or "")
    if memory_scenario:
        return {
            "role": "oikos",
            **memory_scenario,
        }

    is_disk = any(k in text for k in ("disk", "storage", "space"))
    is_cube = "cube" in text
    is_clifford = "clifford" in text
    is_zerg = "zerg" in text
    host_count = sum([is_cube, is_clifford, is_zerg])
    # Heuristic: multi-host disk checks imply parallel intent even without the keyword.
    is_parallel = "parallel" in text or host_count >= 2
    is_math = "2+2" in text or "2 + 2" in text

    # Session continuity / workspace commis scenarios
    is_resume = any(k in text for k in ("resume", "continue session", "pick up where"))
    is_workspace = any(k in text for k in ("workspace", "git repo", "code change", "repository"))
    # Extract session ID if present (UUID-like pattern or explicit session ID mention)
    session_match = re.search(r"session[:\s]+([a-f0-9-]{36})", text) or re.search(r"([a-f0-9-]{36})", text)
    resume_session_id = session_match.group(1) if session_match else None

    if role == "commis":
        if is_disk and (is_cube or is_clifford or is_zerg):
            return {
                "role": "commis",
                "name": "disk_space_commis",
                "evidence_keyword": "45%",
            }
        return None

    # Oikos: workspace commis scenarios (session continuity, git repos)
    if role == "oikos" and (is_resume or is_workspace):
        return {
            "role": "oikos",
            "name": "workspace_commis_oikos",
            "evidence_keyword": "workspace",
            "resume_session_id": resume_session_id,
        }

    if role == "oikos" and is_math:
        return {
            "role": "oikos",
            "name": "math_simple",
            "evidence_keyword": None,
        }

    # Oikos: parallel disk checks first, then single-host disk checks.
    if is_disk and is_parallel:
        return {
            "role": "oikos",
            "name": "disk_space_parallel_oikos",
            "evidence_keyword": "45%",
        }

    # Oikos: match disk check and provide a generic fallback for everything else.
    if is_disk and is_cube:
        return {
            "role": "oikos",
            "name": "disk_space_oikos",
            "evidence_keyword": "45%",
        }

    return {
        "role": "oikos",
        "name": "generic_fallback",
        "evidence_keyword": None,
    }


def get_scenario_evidence_keyword(prompt: str, role: str) -> str | None:
    scenario = find_matching_scenario(prompt, role)
    if not scenario:
        return None
    keyword = scenario.get("evidence_keyword")
    return keyword if isinstance(keyword, str) else None


def _static_response_for_prompt(prompt: str) -> Optional[str]:
    """Return deterministic content for known E2E chat prompts."""
    text = (prompt or "").strip().lower()
    if not text:
        return None

    if "say hello in exactly 10 words" in text:
        # 10 words: Hello(1) there(2) friend(3) this(4) is(5) a(6) ten(7) word(8) greeting(9) today(10).
        return "Hello there friend this is a ten word greeting today."

    if "count from 1 to 5" in text:
        return "1 cat 2 dog 3 fox 4 owl 5"

    if "short sentence about ai" in text:
        return "AI helps people solve problems faster."

    if text in {"hello", "hello!", "hi", "hi!"}:
        return "Hello!"

    if "robot exploring mars" in text:
        return "A small robot trundled across Mars, mapping dunes, sampling rocks, " "and logging the red horizon with steady curiosity."

    return None


def _chunk_text(text: str, *, max_chunks: int = 12) -> List[str]:
    """Split text into a bounded number of deterministic chunks."""
    if not text:
        return []

    words = re.findall(r"\S+\s*", text)
    if len(words) <= 1:
        return [text]

    if len(words) <= max_chunks:
        return words

    chunk_size = max(1, math.ceil(len(words) / max_chunks))
    return ["".join(words[i : i + chunk_size]) for i in range(0, len(words), chunk_size)]


def _extract_callbacks(kwargs: Dict[str, Any]) -> List[Any]:
    """Collect callbacks passed via kwargs or config."""
    callbacks: List[Any] = []
    direct = kwargs.get("callbacks")
    config = kwargs.get("config") or {}
    config_callbacks = config.get("callbacks")

    for item in (direct, config_callbacks):
        if not item:
            continue
        if isinstance(item, (list, tuple)):
            callbacks.extend(item)
        else:
            callbacks.append(item)

    return callbacks


class ScriptedChatLLM(BaseChatModel):
    """A deterministic chat model driven by simple prompt scenarios.

    Supports both static scenarios (default behavior) and sequenced responses
    for testing oikos replay behavior where the LLM might produce different
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

    async def ainvoke(self, messages: List[Any], **kwargs: Any) -> AIMessage:  # noqa: D401 - match BaseChatModel
        """Async invoke returning native AIMessage.

        Bypasses LangChain's internal message coercion so native zerg.types
        messages (including SystemMessage) are accepted in E2E runs.
        """
        # Keep a tiny await to preserve async scheduling semantics.
        await asyncio.sleep(0.0)
        result = self._generate_native(messages)

        callbacks = _extract_callbacks(kwargs)
        if callbacks and isinstance(result, AIMessage):
            content = str(result.content or "")
            if content:
                await self._emit_streaming_tokens(content, callbacks)

        return result

    async def _emit_streaming_tokens(self, content: str, callbacks: List[Any]) -> None:
        """Emit tokens incrementally for streaming UI tests."""
        chunks = _chunk_text(content, max_chunks=12)
        if not chunks:
            return

        delay_seconds = 0.2
        for idx, chunk in enumerate(chunks):
            for callback in callbacks:
                handler = getattr(callback, "on_llm_new_token", None)
                if handler is None:
                    continue
                result = handler(chunk)
                if asyncio.iscoroutine(result):
                    await result
            if idx < len(chunks) - 1:
                await asyncio.sleep(delay_seconds)

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

    def _generate_native(self, messages: List[Any]) -> AIMessage:
        """Generate a native AIMessage without LangChain coercion."""
        role = detect_role_from_messages(messages)
        last_user = next((m for m in reversed(messages) if _is_human_message(m)), None)
        prompt = str(last_user.content) if last_user else ""

        # Check for sequenced response first (enables replay behavior testing)
        sequenced_response = self._get_sequenced_response(prompt)
        if sequenced_response is not None:
            return sequenced_response

        # Check deterministic scenarios EARLY - they are unambiguous and should always
        # trigger their specific tool calls regardless of stale ToolMessages from
        # previous test runs. This prevents flaky tests where leftover tool messages
        # from a prior run cause ScriptedLLM to take the "tool result synthesis" path.
        scenario = find_matching_scenario(prompt, role)
        if scenario and scenario.get("name") == "math_simple":
            return AIMessage(content="4", tool_calls=[])

        # Memory E2E scenarios should also bypass the "tool result" check, but only if
        # the memory tool hasn't already been executed *in this turn* (i.e., after the
        # last HumanMessage). This is needed because the thread persists messages across
        # multiple run_oikos calls.
        if role == "oikos" and scenario and scenario.get("name") == "memory_e2e":
            # Find messages after the last HumanMessage (the current turn)
            last_human_idx = None
            for i, m in enumerate(messages):
                if _is_human_message(m):
                    last_human_idx = i
            current_turn_msgs = messages[last_human_idx + 1 :] if last_human_idx is not None else []

            # Check if there's a ToolMessage in the current turn
            tool_msg_this_turn = next((m for m in current_turn_msgs if _is_tool_message(m)), None)
            if tool_msg_this_turn is not None:
                # A memory tool was already executed in this turn, synthesize result
                content = str(tool_msg_this_turn.content)
                final_text = (
                    "Memory operation completed."
                    if "Memory" in content or "Found" in content or "deleted" in content
                    else "Task completed successfully."
                )
                return AIMessage(content=final_text, tool_calls=[])

            action = scenario.get("action")
            tool_name = None
            args: Dict[str, Any] = {}

            if action == "save":
                tool_name = "save_memory"
                args = {"content": scenario.get("content", "E2E memory content"), "type": "note"}
            elif action == "search":
                tool_name = "search_memory"
                args = {"query": scenario.get("query", "E2E")}
            elif action == "list":
                tool_name = "list_memories"
                args = {"limit": 10}
            elif action == "forget":
                tool_name = "forget_memory"
                memory_id = scenario.get("memory_id") or "00000000-0000-0000-0000-000000000000"
                args = {"memory_id": memory_id}

            if tool_name:
                tool_call = {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": tool_name,
                    "args": args,
                }
                return AIMessage(content="", tool_calls=[tool_call])

        # If tool results are present, emit final synthesis (no more tool calls).
        tool_msg = next((m for m in reversed(messages) if _is_tool_message(m)), None)
        if tool_msg is not None:
            from zerg.tools.result_utils import check_tool_error

            content = str(tool_msg.content)
            is_error, error_msg = check_tool_error(content)

            if is_error:
                final_text = f"Task failed due to tool error: {error_msg or content}"
            else:
                # Determine final text based on scenario type
                if scenario and scenario.get("name") == "workspace_commis_oikos":
                    # Workspace commis completed - summarize the result
                    final_text = "Workspace commis completed successfully. Repository analyzed and changes captured."
                elif "45%" in content:
                    # Disk space check with evidence in tool result
                    final_text = "Cube is at 45% disk usage; biggest usage is Docker images/volumes."
                elif scenario and scenario.get("evidence_keyword") == "45%":
                    # Disk space scenario - inject evidence keyword even if not in tool result
                    final_text = "Cube is at 45% disk usage; biggest usage is Docker images/volumes."
                else:
                    final_text = "Task completed successfully."

            return AIMessage(content=final_text, tool_calls=[])

        static_reply = _static_response_for_prompt(prompt)
        if static_reply:
            return AIMessage(content=static_reply, tool_calls=[])

        # Workspace commis scenario: spawns spawn_workspace_commis with git repo and optional resume
        if role == "oikos" and scenario and scenario.get("name") == "workspace_commis_oikos":
            # Use a public test repo that's small and fast to clone
            args = {
                "task": "Analyze the repository and list the main files",
                "git_repo": "https://github.com/octocat/Hello-World.git",
            }
            # Include resume_session_id if one was extracted from the prompt
            if scenario.get("resume_session_id"):
                args["resume_session_id"] = scenario["resume_session_id"]

            tool_call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": "spawn_workspace_commis",
                "args": args,
            }
            return AIMessage(content="", tool_calls=[tool_call])

        if role == "oikos" and scenario and scenario.get("name") == "disk_space_parallel_oikos":
            tasks = [
                "Check disk space on cube and identify what is using space",
                "Check disk space on clifford and identify what is using space",
                "Check disk space on zerg and identify what is using space",
            ]
            tool_calls = [
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": "spawn_commis",
                    "args": {"task": task},
                }
                for task in tasks
            ]
            return AIMessage(content="", tool_calls=tool_calls)

        if role == "oikos" and scenario and scenario.get("name") == "disk_space_oikos":
            tool_call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": "spawn_commis",
                "args": {"task": "Check disk space on cube and identify what is using space"},
            }
            return AIMessage(content="", tool_calls=[tool_call])

        if role == "commis" and scenario and scenario.get("name") == "disk_space_commis":
            tool_call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": "runner_exec",
                "args": {"target": "cube", "command": "df -h"},
            }
            return AIMessage(content="", tool_calls=[tool_call])

        return AIMessage(content="ok", tool_calls=[])

    def _generate(
        self,
        messages: List[Any],
        stop: Optional[List[str]] = None,  # noqa: ARG002 - unused (deterministic)
        run_manager: Optional[CallbackManagerForLLMRun] = None,  # noqa: ARG002 - unused
        **kwargs: Any,  # noqa: ARG002 - unused
    ) -> ChatResult:
        ai_message = self._generate_native(messages)
        return ChatResult(generations=[ChatGeneration(message=ai_message)])

    async def _agenerate(
        self,
        messages: List[Any],
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
