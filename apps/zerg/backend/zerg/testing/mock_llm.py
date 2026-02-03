"""Mock LLM implementation for testing purposes.

NOTE: This mock still inherits from LangChain's BaseChatModel for interface
compatibility, but uses duck-typing for message checks to work with both
langchain_core.messages and zerg.types.messages.
"""

import asyncio
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


def _is_tool_message(msg: Any) -> bool:
    """Check if message is a tool message (duck-typed)."""
    return getattr(msg, "type", None) == "tool"


def _is_human_message(msg: Any) -> bool:
    """Check if message is a human/user message (duck-typed)."""
    return getattr(msg, "type", None) in ("human", "user")


class MockChatLLM(BaseChatModel):
    """A mock chat LLM that returns predefined responses for testing.

    Uses duck-typing for message checks to work with both langchain_core
    and native zerg.types messages.
    """

    model_name: str = "gpt-mock"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tools = []

    def bind_tools(self, tools, **kwargs):
        """Bind tools to the mock LLM."""
        # Create a copy with the tools bound
        bound = MockChatLLM()
        bound._tools = tools
        return bound

    async def ainvoke(self, messages: List[Any], **kwargs: Any) -> AIMessage:
        """Invoke the LLM asynchronously (native interface).

        This bypasses LangChain's _agenerate and returns native AIMessage directly.
        """
        await asyncio.sleep(0.1)  # Simulate API latency
        return self._generate_native(messages)

    def _generate_native(self, messages: List[Any]) -> AIMessage:
        """Generate response using duck-typed messages, return native AIMessage."""
        import uuid

        # Check for tool results first (continuation)
        tool_msg = next((m for m in reversed(messages) if _is_tool_message(m)), None)
        if tool_msg:
            from zerg.tools.result_utils import check_tool_error

            content = str(tool_msg.content)
            is_error, error_msg = check_tool_error(content)

            if is_error:
                return AIMessage(content=f"Task failed due to tool error: {error_msg or content}")
            else:
                return AIMessage(content="Task completed successfully via commis.")

        # Check last user message for triggers (duck-typed)
        last_msg = next((m for m in reversed(messages) if _is_human_message(m)), None)
        content = str(last_msg.content) if last_msg else ""

        if "TRIGGER_COMMIS" in content:
            # Emit spawn_commis tool call
            tool_call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": "spawn_commis",
                "args": {"task": "Test commis task", "model": "gpt-mock", "wait": False},
            }
            return AIMessage(content="Spawning commis...", tool_calls=[tool_call])

        # Simple mock response
        return AIMessage(content="Hello! I'm a mock assistant. I received your message and I'm responding appropriately.")

    def _generate(self, messages: List[Any], stop: Optional[List[str]] = None, **kwargs: Any) -> ChatResult:
        """LangChain interface - wraps native method."""
        # Import langchain types only when needed for LangChain interface
        from langchain_core.messages import AIMessage as LangChainAIMessage

        native_result = self._generate_native(messages)
        # Convert native AIMessage to LangChain format for backwards compat
        lc_message = LangChainAIMessage(
            content=native_result.content or "",
            tool_calls=native_result.tool_calls or [],
        )
        return ChatResult(generations=[ChatGeneration(message=lc_message)])

    async def _agenerate(
        self,
        messages: List[Any],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """LangChain async interface - wraps native method."""
        await asyncio.sleep(0.1)
        return self._generate(messages, stop, **kwargs)

    @property
    def _llm_type(self) -> str:
        """Return identifier of llm type."""
        return "mock-chat"

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        return {"model_name": self.model_name}
