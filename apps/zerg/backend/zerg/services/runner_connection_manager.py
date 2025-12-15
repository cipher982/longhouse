"""Connection manager for runner WebSocket connections.

Maintains a registry of active runner connections and provides methods
for routing messages to runners.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class RunnerConnectionManager:
    """Singleton manager for runner WebSocket connections.

    Tracks active runner connections and provides message routing.
    Thread-safe for async operations.
    """

    def __init__(self) -> None:
        """Initialize the connection manager."""
        # Key: (owner_id, runner_id), Value: WebSocket connection
        self._connections: Dict[Tuple[int, int], WebSocket] = {}

    def register(self, owner_id: int, runner_id: int, ws: WebSocket) -> None:
        """Register a new runner connection.

        Args:
            owner_id: ID of the runner's owner
            runner_id: ID of the runner
            ws: WebSocket connection
        """
        key = (owner_id, runner_id)

        # If there's an existing connection, it will be replaced
        if key in self._connections:
            logger.warning(
                f"Replacing existing connection for runner {runner_id} (owner {owner_id})"
            )

        self._connections[key] = ws
        logger.info(f"Registered runner {runner_id} (owner {owner_id})")

    def unregister(self, owner_id: int, runner_id: int) -> None:
        """Unregister a runner connection.

        Args:
            owner_id: ID of the runner's owner
            runner_id: ID of the runner
        """
        key = (owner_id, runner_id)
        if key in self._connections:
            del self._connections[key]
            logger.info(f"Unregistered runner {runner_id} (owner {owner_id})")

    def get_connection(self, owner_id: int, runner_id: int) -> Optional[WebSocket]:
        """Get the WebSocket connection for a runner.

        Args:
            owner_id: ID of the runner's owner
            runner_id: ID of the runner

        Returns:
            WebSocket connection if online, None otherwise
        """
        key = (owner_id, runner_id)
        return self._connections.get(key)

    def is_online(self, owner_id: int, runner_id: int) -> bool:
        """Check if a runner is currently connected.

        Args:
            owner_id: ID of the runner's owner
            runner_id: ID of the runner

        Returns:
            True if the runner is connected, False otherwise
        """
        key = (owner_id, runner_id)
        return key in self._connections

    async def send_to_runner(
        self, owner_id: int, runner_id: int, message: Dict[str, Any]
    ) -> bool:
        """Send a JSON message to a runner.

        Args:
            owner_id: ID of the runner's owner
            runner_id: ID of the runner
            message: Message dictionary to send (will be JSON-encoded)

        Returns:
            True if message was sent successfully, False if runner is offline or send failed
        """
        ws = self.get_connection(owner_id, runner_id)
        if not ws:
            logger.warning(
                f"Cannot send message to offline runner {runner_id} (owner {owner_id})"
            )
            return False

        try:
            await ws.send_json(message)
            logger.debug(f"Sent message to runner {runner_id}: {message.get('type')}")
            return True
        except Exception as e:
            logger.error(
                f"Failed to send message to runner {runner_id} (owner {owner_id}): {e}"
            )
            # Connection is broken, unregister it
            self.unregister(owner_id, runner_id)
            return False

    def get_online_count(self, owner_id: Optional[int] = None) -> int:
        """Get count of online runners.

        Args:
            owner_id: If provided, count only runners for this owner

        Returns:
            Number of online runners
        """
        if owner_id is None:
            return len(self._connections)

        return sum(1 for (oid, _) in self._connections.keys() if oid == owner_id)


# Global singleton instance
_manager_instance: Optional[RunnerConnectionManager] = None


def get_runner_connection_manager() -> RunnerConnectionManager:
    """Get the global runner connection manager instance.

    Returns:
        RunnerConnectionManager singleton
    """
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = RunnerConnectionManager()
    return _manager_instance
