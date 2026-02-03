"""Native message types for Longhouse.

Replaces langchain_core.messages with simple dataclasses that match
the OpenAI chat completion message format.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Literal


@dataclass
class BaseMessage:
    """Base class for all message types."""

    content: str | None
    """Message content (text or None for tool-only messages)."""

    @property
    def type(self) -> str:
        """Return the message type for backwards compatibility."""
        return self._type

    _type: str = field(default="base", repr=False)


@dataclass
class SystemMessage(BaseMessage):
    """System message with instructions for the AI."""

    content: str | None = None
    _type: str = field(default="system", repr=False)

    @property
    def type(self) -> Literal["system"]:
        return "system"


@dataclass
class HumanMessage(BaseMessage):
    """Human/user message."""

    content: str | None = None
    _type: str = field(default="human", repr=False)

    @property
    def type(self) -> Literal["human"]:
        return "human"


@dataclass
class AIMessage(BaseMessage):
    """AI/assistant message, optionally with tool calls."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    """List of tool calls in OpenAI format: {id, name, args}."""

    usage_metadata: dict[str, Any] | None = None
    """Token usage metadata from the LLM response."""

    _type: str = field(default="ai", repr=False)

    @property
    def type(self) -> Literal["ai"]:
        return "ai"


@dataclass
class ToolMessage(BaseMessage):
    """Tool result message."""

    content: str | None = None
    tool_call_id: str = ""
    """ID of the tool call this is responding to."""

    name: str | None = None
    """Name of the tool that was called."""

    _type: str = field(default="tool", repr=False)

    @property
    def type(self) -> Literal["tool"]:
        return "tool"


# Type alias for message lists
MessageList = list[BaseMessage]


def to_openai_message(msg: BaseMessage) -> dict[str, Any]:
    """Convert a message to OpenAI chat completion format.

    Args:
        msg: A message instance.

    Returns:
        Dict in OpenAI chat completion message format.
    """
    if isinstance(msg, SystemMessage):
        return {"role": "system", "content": msg.content or ""}

    if isinstance(msg, HumanMessage):
        return {"role": "user", "content": msg.content or ""}

    if isinstance(msg, AIMessage):
        result: dict[str, Any] = {"role": "assistant"}
        if msg.content:
            result["content"] = msg.content
        if msg.tool_calls:
            # Convert to OpenAI tool_calls format
            result["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": tc.get("args", {}),
                    },
                }
                for tc in msg.tool_calls
            ]
        return result

    if isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": msg.content or "",
        }

    # Fallback for unknown types
    return {"role": "user", "content": str(msg.content or "")}


def to_openai_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Convert a list of messages to OpenAI format.

    Args:
        messages: List of message instances.

    Returns:
        List of dicts in OpenAI chat completion format.
    """
    return [to_openai_message(msg) for msg in messages]


def from_openai_message(msg_dict: dict[str, Any]) -> BaseMessage:
    """Convert an OpenAI message dict to a native message.

    Args:
        msg_dict: Dict from OpenAI API response.

    Returns:
        Native message instance.
    """
    role = msg_dict.get("role", "")

    if role == "system":
        return SystemMessage(content=msg_dict.get("content"))

    if role == "user":
        return HumanMessage(content=msg_dict.get("content"))

    if role == "assistant":
        tool_calls = None
        if "tool_calls" in msg_dict:
            # Convert from OpenAI tool_calls format
            tool_calls = [
                {
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "args": tc.get("function", {}).get("arguments", {}),
                }
                for tc in msg_dict.get("tool_calls", [])
            ]
        return AIMessage(
            content=msg_dict.get("content"),
            tool_calls=tool_calls,
        )

    if role == "tool":
        return ToolMessage(
            content=msg_dict.get("content"),
            tool_call_id=msg_dict.get("tool_call_id", ""),
            name=msg_dict.get("name"),
        )

    # Fallback
    return HumanMessage(content=msg_dict.get("content"))
