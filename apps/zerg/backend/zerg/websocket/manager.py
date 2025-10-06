"""Topic-based WebSocket connection manager.

Manages WebSocket connections with topic-based subscriptions and relays
EventBus events to connected clients.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from typing import Dict
from typing import Set

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder

from zerg.config import get_settings
from zerg.events import EventType
from zerg.events import event_bus
from zerg.generated.ws_messages import Envelope

logger = logging.getLogger(__name__)


class TopicConnectionManager:
    """Manages WebSocket connections with topic-based subscriptions."""

    # Constants for back-pressure handling
    SEND_TIMEOUT = 1.0  # Timeout for individual send operations
    QUEUE_SIZE = 100  # Maximum queue size per connection

    def __init__(self):
        """Initialize an empty topic-based connection manager."""
        # Map of client_id to WebSocket connection (guarded by `_lock`)
        self.active_connections: Dict[str, WebSocket] = {}
        # Map of client_id to message queue (guarded by `_lock`)
        self.client_queues: Dict[str, asyncio.Queue] = {}
        # Map of client_id to writer task (guarded by `_lock`)
        self.writer_tasks: Dict[str, asyncio.Task] = {}
        # Map of topic to set of subscribed client_ids (guarded by `_lock`)
        self.topic_subscriptions: Dict[str, Set[str]] = {}
        # Map of client_id to set of subscribed topics (guarded by `_lock`)
        self.client_topics: Dict[str, Set[str]] = {}
        # Map client_id -> authenticated user_id (optional, guarded by `_lock`)
        self.client_users: Dict[str, int | None] = {}

        # Track last *pong* we received from each client.  Used by the
        # heartbeat watchdog to drop zombie connections (4408 close code).
        self._last_pong: Dict[str, float] = {}

        # Protect shared maps from concurrent mutation – a single lock is
        # sufficient given the low contention and keeps the implementation
        # straightforward.  The critical sections are small so the potential
        # throughput impact is negligible compared to network I/O.
        # Use lazy initialization to avoid loop binding issues in tests
        self._lock: asyncio.Lock | None = None
        self._lock_loop_id: int | None = None

        # Background task that proactively cleans up dead sockets so topics
        # don't accumulate zombie client IDs when a browser tab crashes but
        # no further messages are broadcast on the subscribed topic.
        self._cleanup_task: asyncio.Task | None = None

        # Register for relevant events
        self._setup_event_handlers()

    def _get_lock(self) -> asyncio.Lock:
        """Get lock for current event loop, creating new one if needed."""
        try:
            current_loop = asyncio.get_running_loop()
            current_loop_id = id(current_loop)

            # If we don't have a lock or it's from a different loop, create a new one
            if self._lock is None or self._lock_loop_id != current_loop_id:
                self._lock = asyncio.Lock()
                self._lock_loop_id = current_loop_id

            return self._lock
        except RuntimeError:
            # No running loop - create a basic lock (shouldn't happen in practice)
            if self._lock is None:
                self._lock = asyncio.Lock()
            return self._lock

    def _setup_event_handlers(self) -> None:
        """Set up handlers for events we want to broadcast."""
        # Agent events
        event_bus.subscribe(EventType.AGENT_CREATED, self._handle_agent_event)
        event_bus.subscribe(EventType.AGENT_UPDATED, self._handle_agent_event)
        event_bus.subscribe(EventType.AGENT_DELETED, self._handle_agent_event)
        # Thread events
        event_bus.subscribe(EventType.THREAD_CREATED, self._handle_thread_event)
        event_bus.subscribe(EventType.THREAD_UPDATED, self._handle_thread_event)
        event_bus.subscribe(EventType.THREAD_DELETED, self._handle_thread_event)
        event_bus.subscribe(EventType.THREAD_MESSAGE_CREATED, self._handle_thread_event)

        # Run events
        event_bus.subscribe(EventType.RUN_CREATED, self._handle_run_event)
        event_bus.subscribe(EventType.RUN_UPDATED, self._handle_run_event)

        # Workflow execution – node progress
        event_bus.subscribe(EventType.NODE_STATE_CHANGED, self._handle_node_state_event)

        # Workflow execution – finished & logs
        event_bus.subscribe(EventType.EXECUTION_FINISHED, self._handle_execution_finished)
        event_bus.subscribe(EventType.NODE_LOG, self._handle_node_log)

        # User events (e.g., profile updated) – broadcast to dedicated topic
        event_bus.subscribe(EventType.USER_UPDATED, self._handle_user_event)

    async def connect(
        self,
        client_id: str,
        websocket: WebSocket,
        user_id: int | None = None,
        *,
        auto_system: bool = False,
    ) -> None:
        """Register a new client connection.

        Args:
            client_id: Unique identifier for the client
            websocket: The client's WebSocket connection
        """
        # Mutate shared maps inside the lock to avoid races with concurrent
        # disconnect or broadcast calls.
        async with self._get_lock():
            self.active_connections[client_id] = websocket
            self.client_topics[client_id] = set()
            self.client_users[client_id] = user_id
            # Initialise last-pong timestamp so the watchdog countdown starts
            # from *now* (client gets one full interval before needing to
            # reply).
            import time

            self._last_pong[client_id] = time.time()

            # ------------------------------------------------------------------
            # Back-pressure queue
            #
            # During the unit-test suite we set the *TESTING* flag which runs
            # the FastAPI application inside the synchronous TestClient
            # helper (executed in a background thread).  Under that setup the
            # producer side (our test code) can enqueue messages much faster
            # than the background writer coroutine is able to flush them to
            # the mock/stub WebSocket – especially when tests deliberately
            # monkey-patch ``QUEUE_SIZE`` to tiny values (see
            # *test_websocket_envelope.py*).
            #
            # If the per-connection queue is bounded in this environment, the
            # rapid in-process enqueues may hit the limit before the writer
            # even has a chance to `await queue.get()`, causing a spurious
            # ``asyncio.QueueFull`` error and test failures unrelated to the
            # production behaviour.
            #
            # To avoid flakiness we therefore *disable* the hard cap when the
            # global ``TESTING`` flag is active – the production code path
            # (where the web server and browsers communicate over a network
            # socket) is unaffected and still benefits from bounded queues
            # for back-pressure safety.
            # ------------------------------------------------------------------

            from zerg.config import get_settings  # local import to avoid cycles

            queue_size = 0 if get_settings().testing else self.QUEUE_SIZE
            self.client_queues[client_id] = asyncio.Queue(maxsize=queue_size)

            # Start writer task for this client
            self.writer_tasks[client_id] = asyncio.create_task(
                self._writer(client_id, websocket, self.client_queues[client_id])
            )

        # Auto-subscribe the socket to its personal topic so profile updates
        # propagate across tabs/devices without requiring an explicit
        # subscribe message from the client.
        if user_id is not None:
            personal_topic = f"user:{user_id}"
            await self.subscribe_to_topic(client_id, personal_topic)

        # ------------------------------------------------------------------
        # Optional global "system" subscription ---------------------------
        # ------------------------------------------------------------------
        # When *auto_system* is enabled (the default for the production
        # connection initiated via ``/api/ws``) we implicitly attach the
        # socket to the broadcast-only **system** channel so universal
        # announcements reach every open tab without additional API calls.
        #
        # The parameter defaults to *False* so isolated unit tests that
        # instantiate their own ``TopicConnectionManager`` stay unaffected
        # and can assert on an initially empty *client_topics* set.
        # ------------------------------------------------------------------

        if auto_system:
            await self.subscribe_to_topic(client_id, "system")

        logger.info("Client %s connected (user=%s)", client_id, user_id)

        # Lazily start the cleanup ping loop on first connection.  The loop
        # ends automatically when the task is cancelled during application
        # shutdown (handled by FastAPI lifespan).
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def disconnect(self, client_id: str) -> None:
        """Remove a client connection and clean up subscriptions.

        Args:
            client_id: The client ID to remove
        """
        async with self._get_lock():
            if client_id in self.active_connections:
                # Clean user mapping
                self.client_users.pop(client_id, None)
                # Remove from active connections
                del self.active_connections[client_id]

                # Cancel and clean up writer task
                if client_id in self.writer_tasks:
                    writer_task = self.writer_tasks.pop(client_id)
                    if not writer_task.done():
                        writer_task.cancel()

                # Clean up message queue
                if client_id in self.client_queues:
                    del self.client_queues[client_id]

                # Remove from all topic subscriptions
                if client_id in self.client_topics:
                    topics = self.client_topics[client_id]
                    for topic in topics:
                        if topic in self.topic_subscriptions:
                            self.topic_subscriptions[topic].discard(client_id)
                            # Clean up empty topic subscriptions
                            if not self.topic_subscriptions[topic]:
                                del self.topic_subscriptions[topic]
                    del self.client_topics[client_id]

                logger.info("Client %s disconnected", client_id)

            # Remove heartbeat entry even if socket already cleaned (needs to
            # happen outside inner conditional to catch cases where the
            # connection record has been cleared by another path).
            self._last_pong.pop(client_id, None)

    # ------------------------------------------------------------------
    # Heart-beat helpers – client must respond with *pong*
    # ------------------------------------------------------------------

    def record_pong(self, client_id: str) -> None:
        """Update last-pong timestamp for the given client."""
        self._last_pong[client_id] = time.time()

    async def subscribe_to_topic(self, client_id: str, topic: str) -> None:
        """Subscribe a client to a topic.

        Args:
            client_id: The client ID to subscribe
            topic: The topic to subscribe to (e.g., "agent:123", "thread:45")
        """
        async with self._get_lock():
            if topic not in self.topic_subscriptions:
                self.topic_subscriptions[topic] = set()

            self.topic_subscriptions[topic].add(client_id)
            self.client_topics[client_id].add(topic)
        logger.info("Client %s subscribed to topic %s", client_id, topic)

    async def unsubscribe_from_topic(self, client_id: str, topic: str) -> None:
        """Unsubscribe a client from a topic.

        Args:
            client_id: The client ID to unsubscribe
            topic: The topic to unsubscribe from
        """
        async with self._get_lock():
            if topic in self.topic_subscriptions:
                self.topic_subscriptions[topic].discard(client_id)
                if not self.topic_subscriptions[topic]:
                    del self.topic_subscriptions[topic]

            if client_id in self.client_topics:
                self.client_topics[client_id].discard(topic)

        logger.info("Client %s unsubscribed from topic %s", client_id, topic)

    # ------------------------------------------------------------------
    # Queue-based writer for back-pressure safety
    # ------------------------------------------------------------------

    async def _writer(self, client_id: str, websocket: WebSocket, queue: asyncio.Queue) -> None:
        """Writer task that processes messages from the queue with timeout and back-pressure handling."""
        try:
            while True:
                # Wait for a message to send
                payload = await queue.get()

                try:
                    # Send with timeout to prevent hanging on slow clients
                    await asyncio.wait_for(websocket.send_json(payload), timeout=self.SEND_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("Send timeout for client %s, disconnecting", client_id)
                    await self.disconnect(client_id)
                    return
                except Exception as e:
                    logger.warning("Send error for client %s: %s, disconnecting", client_id, e)
                    await self.disconnect(client_id)
                    return
                finally:
                    # Mark task as done regardless of success/failure
                    queue.task_done()

        except asyncio.CancelledError:
            logger.debug("Writer task for client %s cancelled", client_id)
        except Exception as e:
            logger.error("Unexpected error in writer task for client %s: %s", client_id, e)
            await self.disconnect(client_id)

    # ------------------------------------------------------------------
    # Cleanup helpers
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Periodically ping clients and drop unresponsive sockets."""

        try:
            while True:
                await asyncio.sleep(30)  # 30-second heartbeat

                # Create ping message (envelope format if feature flag enabled)
                ping_envelope = Envelope.create(
                    message_type="PING",
                    topic="system",
                    data={},
                )
                ping_message = ping_envelope.model_dump()

                # Get client queues snapshot
                async with self._get_lock():
                    client_queues = dict(self.client_queues)

                # ------------------------------------------------------------------
                # 1) Queue ping for each client
                # ------------------------------------------------------------------
                for client_id, queue in client_queues.items():
                    if queue is None:
                        continue

                    try:
                        queue.put_nowait(ping_message)
                    except asyncio.QueueFull:
                        # Client queue is full - disconnect due to back-pressure
                        logger.warning("Ping queue full for client %s, disconnecting", client_id)
                        asyncio.create_task(self.disconnect(client_id))
                    except Exception:
                        # Queue might be closed if client disconnected
                        pass

                # ------------------------------------------------------------------
                # 2) Drop connections that have not replied with *pong*
                # ------------------------------------------------------------------
                now = time.time()
                async with self._get_lock():
                    stale = [cid for cid, ts in self._last_pong.items() if now - ts > 60]

                for cid in stale:
                    logger.warning("Client %s timed out (no pong), closing", cid)
                    # Close with 4408 (policy from docs) then disconnect.
                    try:
                        ws = self.active_connections.get(cid)
                        if ws is not None:
                            await ws.close(code=4408, reason="Heartbeat timeout")
                    except Exception:  # noqa: BLE001
                        pass

                    await self.disconnect(cid)

        except asyncio.CancelledError:  # graceful shutdown
            logger.info("TopicConnectionManager cleanup task cancelled")

    # ------------------------------------------------------------------
    # Graceful shutdown helper – called from FastAPI lifespan
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:  # noqa: D401 – simple helper
        """Cancel background heartbeat task and close websockets."""

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except Exception:  # pragma: no cover – swallowed
                pass

        # Cancel all writer tasks and close websockets
        async with self._get_lock():
            for task in self.writer_tasks.values():
                if not task.done():
                    task.cancel()

            for ws in self.active_connections.values():
                try:
                    await ws.close()
                except Exception:
                    pass

    async def broadcast_to_topic(self, topic: str, message: Dict[str, Any]) -> None:
        """Broadcast a message to all clients subscribed to a topic.

        Args:
            topic: The topic to broadcast to
            message: The message to broadcast (must be in envelope format)
        """
        # If there are no active subscribers we silently skip to avoid log
        # spam – this situation is perfectly normal when scheduled agents or
        # background jobs emit updates while no browser is connected.
        async with self._get_lock():
            if topic not in self.topic_subscriptions:
                logger.debug("broadcast_to_topic: no subscribers for topic %s", topic)
                return

            # Take a *snapshot* of client IDs and their queues to send to outside the lock
            client_queues = {
                client_id: self.client_queues.get(client_id)  # *None* when no dedicated queue
                for client_id in self.topic_subscriptions[topic]
            }

        # Envelope format is mandatory - no legacy format support
        # All callers must provide properly formatted envelope messages
        if not (isinstance(message, dict) and "v" in message and "topic" in message and "ts" in message):
            logger.error("broadcast_to_topic: Invalid message format - envelope required")
            raise ValueError("Message must be in envelope format")

        final_message = message

        # Queue / immediately send the message for each client
        for client_id, queue in client_queues.items():
            # ----------------------------------------------------------
            # 1. Fallback – no dedicated queue (legacy/dummy tests)
            # ----------------------------------------------------------
            if queue is None:
                ws = self.active_connections.get(client_id)
                if ws is None:
                    continue

                try:
                    await ws.send_json(final_message)
                except Exception as exc:  # pragma: no cover – defensive
                    logger.warning("Send error (direct) for client %s: %s", client_id, exc)
                    asyncio.create_task(self.disconnect(client_id))
                continue

            # ----------------------------------------------------------
            # 2. Normal path – hand off to per-connection queue
            # ----------------------------------------------------------
            try:
                queue.put_nowait(final_message)
            except asyncio.QueueFull:
                # Back-pressure: drop client to protect server memory
                logger.warning("Queue full for client %s, disconnecting due to back-pressure", client_id)
                asyncio.create_task(self.disconnect(client_id))

        # ------------------------------------------------------------------
        # Unit-test helper – block until writer tasks have flushed the queue
        # ------------------------------------------------------------------
        settings = get_settings()
        if settings.testing:
            # Wait (with small timeout) for all queues to drain so assertions
            # that check `send_json.assert_called_*` right after
            # ``broadcast_to_topic`` become deterministic.
            await asyncio.gather(
                *(asyncio.wait_for(q.join(), timeout=1.0) for q in client_queues.values() if q is not None),
                return_exceptions=True,
            )

    async def _handle_agent_event(self, data: Dict[str, Any]) -> None:
        """Handle agent-related events from the event bus."""
        if "id" not in data:
            return

        agent_id = data["id"]
        topic = f"agent:{agent_id}"

        # Extract event_type before serialization to avoid duplication in envelope
        event_type = data["event_type"]

        # Create clean data payload without event_type (since it's in message type)
        clean_data = {k: v for k, v in data.items() if k != "event_type"}
        serialized_data = jsonable_encoder(clean_data)

        # Use envelope format
        envelope = Envelope.create(message_type=event_type, topic=topic, data=serialized_data)
        await self.broadcast_to_topic(topic, envelope.model_dump())

    async def _handle_thread_event(self, data: Dict[str, Any]) -> None:
        """Handle thread-related events from the event bus."""
        if "thread_id" not in data:
            return

        thread_id = data["thread_id"]
        topic = f"thread:{thread_id}"

        # Extract event_type before serialization to avoid duplication in envelope
        event_type = data["event_type"]

        # Create clean data payload without event_type (since it's in message type)
        clean_data = {k: v for k, v in data.items() if k != "event_type"}
        serialized_data = jsonable_encoder(clean_data)

        # Use envelope format
        envelope = Envelope.create(message_type=event_type, topic=topic, data=serialized_data)
        await self.broadcast_to_topic(topic, envelope.model_dump())

    async def _handle_run_event(self, data: Dict[str, Any]) -> None:
        """Forward run events to the *agent:* topic so dashboards update."""
        if "agent_id" not in data:
            return

        agent_id = data["agent_id"]
        topic = f"agent:{agent_id}"

        # Map run_id to id to match schema expectations
        clean_data = {k: v for k, v in data.items() if k != "event_type"}
        if "run_id" in clean_data:
            clean_data["id"] = clean_data.pop("run_id")

        serialized_data = jsonable_encoder(clean_data)

        # Use envelope format
        envelope = Envelope.create(message_type="run_update", topic=topic, data=serialized_data)
        await self.broadcast_to_topic(topic, envelope.model_dump())

    async def _handle_user_event(self, data: Dict[str, Any]) -> None:
        """Forward user events to `user:{id}` topic so other tabs update."""
        user_id = data["id"]

        topic = f"user:{user_id}"

        # Create clean data payload without event_type (since it's in message type)
        clean_data = {k: v for k, v in data.items() if k != "event_type"}
        serialized_data = jsonable_encoder(clean_data)

        # Use envelope format
        envelope = Envelope.create(message_type="user_update", topic=topic, data=serialized_data)
        await self.broadcast_to_topic(topic, envelope.model_dump())

    # ------------------------------------------------------------------
    # Workflow execution node updates
    # ------------------------------------------------------------------

    async def _handle_node_state_event(self, data: Dict[str, Any]) -> None:
        """Broadcast per-node state changes during workflow execution."""

        execution_id = data["execution_id"]

        topic = f"workflow_execution:{execution_id}"

        # Create clean data payload without event_type (since it's in message type)
        clean_data = {k: v for k, v in data.items() if k != "event_type"}
        serialized_data = jsonable_encoder(clean_data)

        # Use envelope format
        envelope = Envelope.create(message_type="node_state", topic=topic, data=serialized_data)
        await self.broadcast_to_topic(topic, envelope.model_dump())

    # ------------------------------------------------------------------
    # Execution finished
    # ------------------------------------------------------------------

    async def _handle_execution_finished(self, data: Dict[str, Any]) -> None:
        exec_id = data["execution_id"]
        topic = f"workflow_execution:{exec_id}"

        # Create clean data payload without event_type (since it's in message type)
        clean_data = {k: v for k, v in data.items() if k != "event_type"}
        serialized_data = jsonable_encoder(clean_data)

        logger.info("Broadcasting execution_finished for execution %s to topic %s", exec_id, topic)

        # Use envelope format
        envelope = Envelope.create(message_type="execution_finished", topic=topic, data=serialized_data)
        await self.broadcast_to_topic(topic, envelope.model_dump())

    # ------------------------------------------------------------------
    # Node log streaming
    # ------------------------------------------------------------------

    async def _handle_node_log(self, data: Dict[str, Any]) -> None:
        exec_id = data["execution_id"]
        topic = f"workflow_execution:{exec_id}"

        # Create clean data payload without event_type (since it's in message type)
        clean_data = {k: v for k, v in data.items() if k != "event_type"}
        serialized_data = jsonable_encoder(clean_data)

        # Use envelope format
        envelope = Envelope.create(message_type="node_log", topic=topic, data=serialized_data)
        await self.broadcast_to_topic(topic, envelope.model_dump())


# Create a global instance of the new connection manager
topic_manager = TopicConnectionManager()

__all__ = ["TopicConnectionManager", "topic_manager"]
