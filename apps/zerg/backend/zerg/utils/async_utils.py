"""Utility for safely running async code from synchronous contexts."""

from typing import Awaitable
from typing import TypeVar

T = TypeVar("T")


def run_async_safely(coroutine: Awaitable[T]) -> T:
    """Run an async coroutine safely from a synchronous context.

    This function is deprecated. Use the shared async runner instead:
        from zerg.utils.async_runner import run_in_shared_loop
        result = run_in_shared_loop(coroutine)

    Args:
        coroutine: The coroutine to execute

    Returns:
        The result of the coroutine
    """
    from zerg.utils.async_runner import run_in_shared_loop
    return run_in_shared_loop(coroutine)
