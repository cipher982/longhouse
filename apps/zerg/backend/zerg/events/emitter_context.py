"""Contextvar-based emitter transport.

This module provides a single contextvar for passing emitters through the async
call stack. Unlike the old pattern where contextvars determined event *type*,
here the contextvar only *transports* an emitter whose identity is already fixed.

Usage:
    # At entry point (worker_runner.py, supervisor_service.py):
    from zerg.events import WorkerEmitter, set_emitter, reset_emitter

    emitter = WorkerEmitter(worker_id=..., ...)
    token = set_emitter(emitter)
    try:
        await agent.run()
    finally:
        reset_emitter(token)

    # In tool execution (zerg_react_agent.py):
    from zerg.events import get_emitter

    emitter = get_emitter()
    if emitter:
        await emitter.emit_tool_started(...)

Why This Is Safe:
    Even if asyncio.create_task() copies the contextvar, the emitter object's
    identity (worker vs supervisor) is fixed at construction. A WorkerEmitter
    will always emit worker_tool_* events.
"""

from __future__ import annotations

from contextvars import ContextVar
from contextvars import Token
from typing import TYPE_CHECKING
from typing import Optional
from typing import Union

if TYPE_CHECKING:
    from zerg.events.null_emitter import NullEmitter
    from zerg.events.supervisor_emitter import SupervisorEmitter
    from zerg.events.worker_emitter import WorkerEmitter

    EmitterType = Union[WorkerEmitter, SupervisorEmitter, NullEmitter]

# Single contextvar for emitter transport
# The emitter's identity is baked in at construction time
_emitter_var: ContextVar[Optional["EmitterType"]] = ContextVar("_emitter_var", default=None)


def get_emitter() -> Optional["EmitterType"]:
    """Get the current emitter, if any.

    Returns:
        The current emitter (WorkerEmitter, SupervisorEmitter, or NullEmitter),
        or None if not in an emitter context.

    Usage:
        emitter = get_emitter()
        if emitter:
            await emitter.emit_tool_started(tool_name, tool_call_id, args_preview)
    """
    return _emitter_var.get()


def set_emitter(emitter: "EmitterType") -> Token[Optional["EmitterType"]]:
    """Set the emitter for the current async context.

    Must be paired with reset_emitter() in a finally block.

    Args:
        emitter: The emitter to set (WorkerEmitter, SupervisorEmitter, or NullEmitter)

    Returns:
        Token for resetting via reset_emitter()

    Usage:
        emitter = WorkerEmitter(...)
        token = set_emitter(emitter)
        try:
            await agent.run()
        finally:
            reset_emitter(token)
    """
    return _emitter_var.set(emitter)


def reset_emitter(token: Token[Optional["EmitterType"]]) -> None:
    """Reset the emitter to its previous value.

    Args:
        token: Token returned by set_emitter()
    """
    _emitter_var.reset(token)


__all__ = ["get_emitter", "set_emitter", "reset_emitter"]
