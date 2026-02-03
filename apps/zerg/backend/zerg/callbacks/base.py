"""Native callback interfaces for Longhouse.

Replaces langchain_core.callbacks with simple async callback protocols.
"""

from __future__ import annotations

from typing import Any


class AsyncTokenCallback:
    """Base class for async token streaming callbacks.

    Implement on_llm_new_token to receive individual tokens during
    streaming LLM responses.
    """

    async def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        """Called for each new token during streaming.

        Args:
            token: The new token string.
            **kwargs: Additional metadata (unused, for extension).
        """
        pass


# Alias for backwards compatibility
AsyncCallbackHandler = AsyncTokenCallback

__all__ = ["AsyncTokenCallback", "AsyncCallbackHandler"]
