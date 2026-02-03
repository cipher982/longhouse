"""Native OpenAI client wrapper for Longhouse.

Replaces ChatOpenAI from langchain_openai with direct OpenAI SDK calls.
Provides tool binding, streaming, and usage tracking.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from openai import AsyncOpenAI

from zerg.types.messages import AIMessage
from zerg.types.messages import BaseMessage
from zerg.types.messages import HumanMessage
from zerg.types.messages import SystemMessage
from zerg.types.messages import ToolMessage

if TYPE_CHECKING:
    from zerg.types.tools import Tool

logger = logging.getLogger(__name__)


@dataclass
class ChatResponse:
    """Response from a chat completion call."""

    message: AIMessage
    """The AI message response."""

    usage: dict[str, Any] = field(default_factory=dict)
    """Token usage information."""

    finish_reason: str | None = None
    """Why the model stopped generating."""


def _convert_message_to_openai(msg: BaseMessage) -> dict[str, Any]:
    """Convert a native message to OpenAI API format.

    This handles the specific field names and structures expected by
    the OpenAI API.
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
            result["tool_calls"] = []
            for tc in msg.tool_calls:
                args = tc.get("args", {})
                # Args may be dict or already a string
                if isinstance(args, dict):
                    args_str = json.dumps(args)
                else:
                    args_str = str(args)

                result["tool_calls"].append(
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": args_str,
                        },
                    }
                )
        return result

    if isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": msg.content or "",
        }

    # Fallback
    return {"role": "user", "content": str(msg.content or "")}


def _parse_openai_response(response) -> ChatResponse:
    """Parse an OpenAI chat completion response into our format."""
    choice = response.choices[0]
    message = choice.message

    # Extract tool calls
    tool_calls = None
    if message.tool_calls:
        tool_calls = []
        for tc in message.tool_calls:
            args = tc.function.arguments
            # Parse args if it's a JSON string
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass  # Keep as string if not valid JSON

            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": args,
                }
            )

    # Build AIMessage
    ai_message = AIMessage(
        content=message.content,
        tool_calls=tool_calls,
    )

    # Extract usage
    usage = {}
    if response.usage:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }
        # Extract reasoning tokens if available
        if hasattr(response.usage, "completion_tokens_details"):
            details = response.usage.completion_tokens_details
            if details and hasattr(details, "reasoning_tokens"):
                usage["completion_tokens_details"] = {
                    "reasoning_tokens": details.reasoning_tokens or 0,
                }

    # Store usage in message for compatibility
    ai_message.usage_metadata = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "output_token_details": {
            "reasoning": usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
        },
    }

    return ChatResponse(
        message=ai_message,
        usage=usage,
        finish_reason=choice.finish_reason,
    )


class OpenAIChat:
    """Native OpenAI chat client with tool support.

    Replaces LangChain's ChatOpenAI with direct SDK calls.

    Usage:
        client = OpenAIChat(model="gpt-4", api_key=api_key)
        bound = client.bind_tools(tools)
        response = await bound.ainvoke(messages)
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        streaming: bool = False,
        reasoning_effort: str | None = None,
    ):
        """Initialize the OpenAI chat client.

        Args:
            model: Model name (e.g., "gpt-4", "gpt-3.5-turbo").
            api_key: OpenAI API key. Uses OPENAI_API_KEY env var if not provided.
            base_url: Optional base URL for API (for Groq, etc.).
            streaming: Whether to stream responses.
            reasoning_effort: Reasoning effort for o1/o3 models ("low", "medium", "high").
        """
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._streaming = streaming
        self._reasoning_effort = reasoning_effort
        self._tools: list[Tool] = []
        self._tool_choice: dict | str | None = None

        # Create async client
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    def bind_tools(
        self,
        tools: list[Tool],
        *,
        tool_choice: dict | str | None = None,
    ) -> "OpenAIChat":
        """Create a copy with tools bound.

        Args:
            tools: List of Tool instances to bind.
            tool_choice: Tool choice mode ("auto", "required", "none", or specific tool).

        Returns:
            New OpenAIChat instance with tools bound.
        """
        bound = OpenAIChat(
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
            streaming=self._streaming,
            reasoning_effort=self._reasoning_effort,
        )
        bound._tools = tools
        bound._tool_choice = tool_choice
        bound._client = self._client  # Share client
        return bound

    def _get_tools_param(self) -> list[dict[str, Any]] | None:
        """Get tools in OpenAI format."""
        if not self._tools:
            return None

        return [tool.to_openai_tool() for tool in self._tools]

    def _get_tool_choice_param(self) -> str | dict | None:
        """Get tool_choice parameter."""
        if self._tool_choice is None:
            return "auto" if self._tools else None

        if isinstance(self._tool_choice, bool):
            return "required" if self._tool_choice else "none"

        return self._tool_choice

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        *,
        config: dict | None = None,
    ) -> AIMessage:
        """Invoke the model asynchronously.

        Args:
            messages: List of messages to send.
            config: Optional config dict (for callbacks, etc.).

        Returns:
            AIMessage with response content and optional tool calls.
        """
        # Convert messages to OpenAI format
        openai_messages = [_convert_message_to_openai(m) for m in messages]

        # Build request parameters
        params: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
        }

        # Add tools if bound
        tools_param = self._get_tools_param()
        if tools_param:
            params["tools"] = tools_param
            params["tool_choice"] = self._get_tool_choice_param()

        # Add reasoning effort for supported models
        if self._reasoning_effort and self._reasoning_effort != "none":
            params["reasoning_effort"] = self._reasoning_effort

        # Handle streaming
        if self._streaming and config and config.get("callbacks"):
            return await self._stream_with_callbacks(params, config["callbacks"])

        # Non-streaming call
        response = await self._client.chat.completions.create(**params)
        result = _parse_openai_response(response)
        return result.message

    async def _stream_with_callbacks(
        self,
        params: dict[str, Any],
        callbacks: list,
    ) -> AIMessage:
        """Stream response and invoke callbacks for each token.

        Args:
            params: Request parameters for OpenAI API.
            callbacks: List of callback handlers.

        Returns:
            Complete AIMessage after streaming.
        """
        params["stream"] = True
        params["stream_options"] = {"include_usage": True}

        collected_content = ""
        collected_tool_calls: list[dict] = []
        usage = {}

        async with await self._client.chat.completions.create(**params) as stream:
            async for chunk in stream:
                if not chunk.choices:
                    # Usage info comes in final chunk without choices
                    if chunk.usage:
                        usage = {
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                            "total_tokens": chunk.usage.total_tokens,
                        }
                    continue

                delta = chunk.choices[0].delta

                # Handle content tokens
                if delta.content:
                    collected_content += delta.content
                    # Invoke token callbacks
                    for cb in callbacks:
                        if hasattr(cb, "on_llm_new_token"):
                            try:
                                await cb.on_llm_new_token(delta.content)
                            except Exception as e:
                                logger.warning(f"Token callback error: {e}")

                # Handle tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        # Expand list if needed
                        while len(collected_tool_calls) <= idx:
                            collected_tool_calls.append(
                                {
                                    "id": "",
                                    "name": "",
                                    "args": "",
                                }
                            )
                        # Accumulate tool call parts
                        if tc.id:
                            collected_tool_calls[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                collected_tool_calls[idx]["name"] = tc.function.name
                            if tc.function.arguments:
                                collected_tool_calls[idx]["args"] += tc.function.arguments

        # Parse accumulated tool call arguments
        final_tool_calls = None
        if collected_tool_calls:
            final_tool_calls = []
            for tc in collected_tool_calls:
                args = tc["args"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                final_tool_calls.append(
                    {
                        "id": tc["id"],
                        "name": tc["name"],
                        "args": args,
                    }
                )

        # Build AIMessage
        ai_message = AIMessage(
            content=collected_content or None,
            tool_calls=final_tool_calls,
        )

        # Store usage metadata
        ai_message.usage_metadata = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

        return ai_message


def create_openai_chat(
    model: str,
    tools: list[Tool],
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    streaming: bool = False,
    reasoning_effort: str | None = None,
    tool_choice: dict | str | None = None,
) -> OpenAIChat:
    """Factory function to create an OpenAI chat client with tools.

    This is the main entry point for creating a chat client. It replaces
    the LangChain pattern of ChatOpenAI().bind_tools().

    Args:
        model: Model name.
        tools: List of tools to bind.
        api_key: OpenAI API key.
        base_url: Optional base URL.
        streaming: Enable streaming.
        reasoning_effort: Reasoning effort for o1/o3 models.
        tool_choice: Tool choice mode.

    Returns:
        OpenAIChat instance with tools bound.
    """
    client = OpenAIChat(
        model=model,
        api_key=api_key,
        base_url=base_url,
        streaming=streaming,
        reasoning_effort=reasoning_effort,
    )
    return client.bind_tools(tools, tool_choice=tool_choice)
