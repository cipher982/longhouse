"""Utility for safely running async code from synchronous contexts."""

import asyncio
import threading
from typing import Awaitable, TypeVar

T = TypeVar("T")


def run_async_safely(coroutine: Awaitable[T]) -> T:
    """Run an async coroutine safely from a synchronous context.

    Handles cases where:
    1. No event loop exists (creates one via asyncio.run)
    2. An event loop is already running (uses run_coroutine_threadsafe)

    Args:
        coroutine: The coroutine to execute

    Returns:
        The result of the coroutine
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside a running loop (e.g., LangChain/FastAPI)
        # We cannot use asyncio.run() or loop.run_until_complete()
        # We must delegate to a separate thread to block safely
        
        # Create a new loop in a separate thread
        result_container = {}
        exception_container = {}
        event = threading.Event()

        def _run_in_thread():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result_container["value"] = new_loop.run_until_complete(coroutine)
            except Exception as e:
                exception_container["error"] = e
            finally:
                new_loop.close()
                event.set()

        thread = threading.Thread(target=_run_in_thread)
        thread.start()
        thread.join()
        
        if "error" in exception_container:
            raise exception_container["error"]
            
        return result_container["value"]
    else:
        # No running loop, use asyncio.run()
        return asyncio.run(coroutine)

