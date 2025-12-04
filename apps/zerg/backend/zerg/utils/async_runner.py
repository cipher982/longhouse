"""Shared Async Runner – centralized async execution for synchronous contexts.

This module provides a single, shared async runner that consolidates all
cross-context async execution. Instead of each service creating its own event
loop or using run_async_safely, all services should use this shared runner.

Benefits:
- Single point of control for async execution
- Proper resource management
- Consistent error handling
- Prevents loop conflicts and resource leaks
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Optional

logger = logging.getLogger(__name__)


class SharedAsyncRunner:
    """Singleton async runner for executing coroutines from sync contexts."""

    _instance: Optional["SharedAsyncRunner"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "SharedAsyncRunner":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._loop: Optional[asyncio.AbstractEventLoop] = None
                cls._instance._thread: Optional[threading.Thread] = None
                cls._instance._running = False
                cls._instance._shutdown_event = threading.Event()
            return cls._instance

    def start(self) -> None:
        """Start the shared async runner."""
        with self._lock:
            if self._running:
                logger.warning("Shared async runner already running")
                return

            self._running = True
            self._shutdown_event.clear()
            self._loop = asyncio.new_event_loop()

            def run_loop():
                asyncio.set_event_loop(self._loop)
                try:
                    logger.info("Shared async runner loop started")
                    self._loop.run_until_complete(self._run_forever())
                except Exception as e:
                    logger.exception(f"Shared async runner loop failed: {e}")
                finally:
                    logger.info("Shared async runner loop stopped")

            self._thread = threading.Thread(target=run_loop, daemon=True, name="shared-async-runner")
            self._thread.start()

    def stop(self) -> None:
        """Stop the shared async runner."""
        with self._lock:
            if not self._running:
                return

            self._running = False
            self._shutdown_event.set()

            if self._loop:
                self._loop.call_soon_threadsafe(self._loop.stop)

            if self._thread:
                self._thread.join(timeout=5.0)
                if self._thread.is_alive():
                    logger.warning("Shared async runner thread did not stop gracefully")

    async def _run_forever(self) -> None:
        """Internal loop that runs until shutdown."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(0.1)

    def run_coroutine(self, coro: Awaitable[Any]) -> Any:
        """Run a coroutine in the shared loop.

        If the shared runner is not started, falls back to:
        1. asyncio.run() if no event loop is running
        2. A temporary thread with its own event loop if already inside an event loop

        This fallback ensures scripts, tests, and CLI tools continue to work
        without explicitly starting the shared runner.

        Args:
            coro: The coroutine to execute

        Returns:
            The result of the coroutine

        Raises:
            Exception: Any exception raised by the coroutine
        """
        # If shared runner is running, use it
        if self._running and self._loop:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result()

        # Fallback for non-server contexts (tests, scripts, CLI)
        return self._run_coroutine_fallback(coro)

    def _run_coroutine_fallback(self, coro: Awaitable[Any]) -> Any:
        """Fallback for running coroutines when shared runner isn't started."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're inside a running loop - use a temporary thread
            result_container: dict = {}
            exception_container: dict = {}

            def _run_in_thread():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    result_container["value"] = new_loop.run_until_complete(coro)
                except Exception as e:
                    exception_container["error"] = e
                finally:
                    new_loop.close()

            thread = threading.Thread(target=_run_in_thread)
            thread.start()
            thread.join()

            if "error" in exception_container:
                raise exception_container["error"]

            return result_container["value"]
        else:
            # No running loop - use asyncio.run()
            return asyncio.run(coro)

    def run_sync(self, func: Callable[[], Any]) -> Any:
        """Run a synchronous function in the shared loop using to_thread.

        Args:
            func: The synchronous function to execute

        Returns:
            The result of the function
        """
        async def _run():
            return await asyncio.to_thread(func)

        return self.run_coroutine(_run())


# Global instance
_shared_runner = SharedAsyncRunner()


def get_shared_runner() -> SharedAsyncRunner:
    """Get the global shared async runner instance."""
    return _shared_runner


def run_in_shared_loop(coro: Awaitable[Any]) -> Any:
    """Run a coroutine in the shared async loop.

    This is the main entry point for running async code from sync contexts.

    Args:
        coro: The coroutine to execute

    Returns:
        The result of the coroutine
    """
    return _shared_runner.run_coroutine(coro)


def run_sync_in_shared_loop(func: Callable[[], Any]) -> Any:
    """Run a synchronous function in the shared async loop.

    Args:
        func: The synchronous function to execute

    Returns:
        The result of the function
    """
    return _shared_runner.run_sync(func)
