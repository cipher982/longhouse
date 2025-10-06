"""Mock LLM implementation for testing purposes."""

import asyncio
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult


class MockChatLLM(BaseChatModel):
    """A mock chat LLM that returns predefined responses for testing."""

    model_name: str = "gpt-mock"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tools = []

    def bind_tools(self, tools):
        """Bind tools to the mock LLM."""
        # Create a copy with the tools bound
        bound = MockChatLLM()
        bound._tools = tools
        return bound

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate a chat response synchronously."""
        # Simple mock response
        response_text = "Hello! I'm a mock assistant. I received your message and I'm responding appropriately."

        # Create the AI message
        ai_message = AIMessage(content=response_text)

        # Create chat generation
        generation = ChatGeneration(message=ai_message)

        return ChatResult(generations=[generation])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate a chat response asynchronously."""
        # Add a small delay to simulate real API call
        await asyncio.sleep(0.1)

        # For now, just call the sync version
        return self._generate(messages, stop, run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        """Return identifier of llm type."""
        return "mock-chat"

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        return {"model_name": self.model_name}
