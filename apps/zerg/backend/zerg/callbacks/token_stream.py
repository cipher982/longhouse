"""WebSocket token callback utilities.

This module provides an ``AsyncCallbackHandler`` implementation that streams
individual **assistant tokens** over the existing topic-based WebSocket layer.

The handler is *stateless* apart from a reference to the global
``topic_manager`` instance.  The thread context is passed via a
``contextvars.ContextVar`` so callers (namely ``AgentRunner``) can set/reset
the value immediately before invoking the LangGraph runnable without having to
re-instantiate the LLM for each thread.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any
from typing import Optional

# Optional import to keep the runtime lightweight in test environments where
# the full ``langchain`` package might not be installed (only
# ``langchain-core`` is a dependency).  We fall back to a minimal base-class
# replacement that matches the few attributes we rely on.
# Single, canonical import – the stack always includes langchain-core.
# Fallbacks are deliberately avoided to keep behaviour deterministic.
from langchain_core.callbacks.base import AsyncCallbackHandler  # type: ignore

from zerg.generated.ws_messages import Envelope
from zerg.generated.ws_messages import StreamChunkData
from zerg.websocket.manager import topic_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context variables – the *current* thread and user being processed.
# ---------------------------------------------------------------------------


current_thread_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(  # noqa: E501
    "current_thread_id_var",
    default=None,
)

current_user_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(  # noqa: E501
    "current_user_id_var",
    default=None,
)

# ---------------------------------------------------------------------------
# Callback implementation
# ---------------------------------------------------------------------------


class WsTokenCallback(AsyncCallbackHandler):
    """LangChain callback that forwards every new LLM token over WebSocket."""

    def __init__(self, **kwargs: Any) -> None:
        """Initialize callback with skip-warning flag to prevent log spam."""
        super().__init__(**kwargs)
        # Track if we've already warned about missing context (avoid log spam)
        self._warned_no_context = False

    # We only need **async** token notifications

    # ------------------------------------------------------------------
    # Callback filtering
    # ------------------------------------------------------------------

    # We *only* care about individual LLM tokens.  All other event categories
    # (chains, agents, chat-model lifecycle, etc.) can be safely ignored to
    # avoid unnecessary method dispatch and the corresponding "method not
    # implemented" errors that were cluttering the logs.  The
    # ``BaseCallbackHandler`` contract allows us to opt-out on a per-category
    # basis by overriding the ``ignore_*`` properties.

    @property  # type: ignore[override]
    def ignore_chain(self) -> bool:  # noqa: D401 – property mirrors base-class naming
        return True

    @property  # type: ignore[override]
    def ignore_agent(self) -> bool:  # noqa: D401
        return True

    @property  # type: ignore[override]
    def ignore_retriever(self) -> bool:  # noqa: D401
        return True

    @property  # type: ignore[override]
    def ignore_chat_model(self) -> bool:  # noqa: D401
        # We already receive token-level callbacks via ``on_llm_new_token``
        # even for chat models, so we can skip the separate chat-specific
        # lifecycle hooks entirely.
        return True

    @property  # type: ignore[override]
    def ignore_custom_event(self) -> bool:  # noqa: D401
        return True

    def __init__(self) -> None:  # noqa: D401 – keep signature minimal
        super().__init__()

    # ------------------------------------------------------------------
    # LLM token hook
    # ------------------------------------------------------------------

    async def on_llm_new_token(self, token: str, **_: Any) -> None:  # noqa: D401 – interface defined by LangChain
        """Broadcast *token* to all subscribers of the current user topic."""

        thread_id = current_thread_id_var.get()
        user_id = current_user_id_var.get()

        if thread_id is None or user_id is None:
            # If no context is set we skip – this can happen if the LLM is
            # called outside an ``AgentRunner`` (unit-tests, workers, etc.).
            # Only warn once per callback instance to prevent log spam.
            if not self._warned_no_context:
                logger.debug("WsTokenCallback: thread_id or user_id context not set – skipping token dispatch")
                self._warned_no_context = True
            return

        topic = f"user:{user_id}"

        try:
            chunk_data = StreamChunkData(
                thread_id=thread_id,
                content=token,
                chunk_type="assistant_token",
                tool_name=None,
                tool_call_id=None,
            )
            envelope = Envelope.create(
                message_type="stream_chunk",
                topic=topic,
                data=chunk_data.model_dump(),
            )
            await topic_manager.broadcast_to_topic(topic, envelope.model_dump())
        except Exception:  # noqa: BLE001 – we log then swallow; token streaming is best-effort
            logger.exception("Error broadcasting token chunk for user %s, thread %s", user_id, thread_id)


# ---------------------------------------------------------------------------
# Convenience helpers used by AgentRunner
# ---------------------------------------------------------------------------


def set_current_thread_id(thread_id: int | None):  # noqa: D401 – tiny setter helper
    """Set *thread_id* as the active context for token streaming.

    Returns the *Token* object from ``ContextVar.set`` so callers can restore
    the previous value via ``ContextVar.reset`` if desired.
    """

    if thread_id is None:
        return current_thread_id_var.set(None)

    return current_thread_id_var.set(int(thread_id))


def reset_current_thread_id(token: contextvars.Token) -> None:
    """Reset the thread_id context using a token from set_current_thread_id.

    This ensures we restore the context to exactly what it was before,
    rather than arbitrarily clearing it.
    """
    current_thread_id_var.reset(token)


def set_current_user_id(user_id: int | None):  # noqa: D401 – tiny setter helper
    """Set *user_id* as the active context for token streaming.

    Returns the *Token* object from ``ContextVar.set`` so callers can restore
    the previous value via ``ContextVar.reset`` if desired.
    """

    if user_id is None:
        return current_user_id_var.set(None)

    return current_user_id_var.set(int(user_id))


__all__ = [
    "WsTokenCallback",
    "current_thread_id_var",
    "current_user_id_var",
    "set_current_thread_id",
    "reset_current_thread_id",
    "set_current_user_id",
]
