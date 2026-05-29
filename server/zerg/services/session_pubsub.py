"""Per-topic pubsub for session-scoped realtime fanout.

Replaces the global-wake pattern (`WriteSerializer._notify_change` waking
every SSE subscriber on every write). Each SSE stream subscribes to one
or more topics and only wakes when a relevant event was published.

Topics:
- ``session:{session_id}`` — per-session events (ingest, runtime, actions)
- ``timeline`` — any event relevant to the timeline list
- ``session_thread:{thread_root_session_id}`` — (reserved, future use)

Payloads are small JSON-serializable dicts. SSE loops translate them to
frame-specific shapes. Bounded queues drop-oldest to keep a slow subscriber
from blocking publishers; the drop count surfaces via Prometheus.

This is a process-local pubsub. Multi-worker deployments need a cross-worker
fan-out path (Redis, Postgres LISTEN, etc.) before this helps at scale;
today's Longhouse runtime is single-worker so that's fine.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PubsubMessage:
    """Lightweight envelope. `seq` is a per-topic monotonic cursor usable for Last-Event-ID."""

    seq: int
    topic: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ReplayGap:
    """Replay cursor cannot be satisfied from the process-local topic buffer."""

    requested_seq: int
    earliest_seq: int | None
    latest_seq: int
    reason: str


class _Subscriber:
    __slots__ = ("queue", "drops")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[PubsubMessage] = asyncio.Queue(maxsize=maxsize)
        self.drops: int = 0


@dataclass
class _TopicState:
    subscribers: set[_Subscriber] = field(default_factory=set)
    buffer: deque[PubsubMessage] = field(default_factory=lambda: deque(maxlen=1000))
    next_seq: int = 1


class SessionPubsub:
    """Process-local topic pubsub with per-topic replay buffer.

    Thread-unsafe; intended for use from the asyncio event loop. All
    subscribe/publish/unsubscribe calls must be made from the same loop.
    """

    def __init__(self, *, subscriber_queue_size: int = 256, buffer_size: int = 1000) -> None:
        self._topics: dict[str, _TopicState] = defaultdict(_TopicState)
        self._subscriber_queue_size = subscriber_queue_size
        self._buffer_size = buffer_size

    # ------------------------------------------------------------------ publish
    def publish(self, topic: str, payload: dict[str, Any]) -> int:
        """Publish a message to a topic. Returns the assigned seq."""
        state = self._topics[topic]
        if state.buffer.maxlen != self._buffer_size:
            # First-touch sizing for defaultdict-created states.
            state.buffer = deque(state.buffer, maxlen=self._buffer_size)
        msg = PubsubMessage(seq=state.next_seq, topic=topic, payload=payload)
        state.next_seq += 1
        state.buffer.append(msg)

        for sub in list(state.subscribers):
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Drop-oldest: remove one, try again. If still full, count drop.
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(msg)
                    sub.drops += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    sub.drops += 1
        return msg.seq

    # ---------------------------------------------------------------- subscribe
    def subscribe(self, topic: str, *, since_seq: int | None = None) -> "_Subscription":
        """Subscribe to a topic. If since_seq is provided, queue buffered replay first."""
        state = self._topics[topic]
        sub = _Subscriber(self._subscriber_queue_size)

        if since_seq is not None:
            for msg in state.buffer:
                if msg.seq > since_seq:
                    try:
                        sub.queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        # Replay filled the queue; caller must drain before subscribing live.
                        break

        state.subscribers.add(sub)
        return _Subscription(self, topic, sub)

    def replay_gap(self, topic: str, *, since_seq: int | None) -> ReplayGap | None:
        """Return gap metadata when `since_seq` cannot be replayed faithfully.

        Pubsub seqs are process-local. If a client reconnects with a cursor from
        an older process, or with a cursor older than the bounded ring, the live
        stream must say so explicitly so clients can reconcile from durable DB
        state instead of assuming the replay lane was complete.
        """
        if since_seq is None or since_seq <= 0:
            return None

        state = self._topics[topic]
        latest_seq = state.buffer[-1].seq if state.buffer else max(0, state.next_seq - 1)
        if not state.buffer:
            return ReplayGap(
                requested_seq=since_seq,
                earliest_seq=None,
                latest_seq=latest_seq,
                reason="buffer_unavailable",
            )

        earliest_seq = state.buffer[0].seq
        if since_seq < earliest_seq - 1:
            return ReplayGap(
                requested_seq=since_seq,
                earliest_seq=earliest_seq,
                latest_seq=latest_seq,
                reason="cursor_too_old",
            )
        if since_seq > latest_seq:
            return ReplayGap(
                requested_seq=since_seq,
                earliest_seq=earliest_seq,
                latest_seq=latest_seq,
                reason="cursor_ahead",
            )
        return None

    # -------------------------------------------------------------------- peek
    def peek_latest_seq(self, topic: str) -> int:
        """Return the most recent seq for a topic (0 if no messages yet)."""
        state = self._topics.get(topic)
        if not state or not state.buffer:
            return 0
        return state.buffer[-1].seq

    def _unsubscribe(self, topic: str, sub: _Subscriber) -> None:
        state = self._topics.get(topic)
        if state is None:
            return
        state.subscribers.discard(sub)


class _Subscription:
    """RAII-style subscription handle. Use `async for msg in sub:` or explicit next_message()."""

    def __init__(self, bus: SessionPubsub, topic: str, sub: _Subscriber) -> None:
        self._bus = bus
        self._topic = topic
        self._sub = sub
        self._closed = False

    @property
    def drops(self) -> int:
        return self._sub.drops

    async def next_message(self, timeout: float | None = None) -> PubsubMessage | None:
        """Wait for the next message. Returns None on timeout."""
        if self._closed:
            return None
        try:
            if timeout is None:
                return await self._sub.queue.get()
            return await asyncio.wait_for(self._sub.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self._topic, self._sub)

    def __aenter__(self):  # type: ignore[override]
        raise NotImplementedError("Use `with bus.subscribe(...) as sub` sync-style")

    def __enter__(self) -> "_Subscription":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# -----------------------------------------------------------------------------
# Process-wide singleton
# -----------------------------------------------------------------------------

_bus: SessionPubsub | None = None


def get_pubsub() -> SessionPubsub:
    global _bus
    if _bus is None:
        _bus = SessionPubsub()
    return _bus


def reset_pubsub_for_test() -> None:
    global _bus
    _bus = None


def topic_session(session_id: str) -> str:
    return f"session:{session_id}"


TOPIC_TIMELINE = "timeline"


def publish_session_runtime_update(
    *,
    session_id: str,
    provider: str | None,
    source: str | None,
) -> None:
    """Wake session and timeline subscribers after persisted runtime state changes."""
    payload = {
        "kind": "runtime",
        "session_id": session_id,
        "provider": provider,
        "source": source,
        "server_fanout_at_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    bus = get_pubsub()
    bus.publish(topic_session(session_id), payload)
    bus.publish(TOPIC_TIMELINE, payload)


def publish_session_transcript_preview_update(
    *,
    session_id: str,
    provider: str | None,
    source: str | None,
    transcript_preview: dict,
) -> None:
    """Wake the focused session workspace with a live transcript preview.

    This is intentionally session-scoped. The durable runtime observation write
    will publish the normal session/timeline update after SQLite catches up.
    """
    payload = {
        "kind": "transcript_preview",
        "session_id": session_id,
        "provider": provider,
        "source": source,
        "server_fanout_at_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
        "transcript_preview": transcript_preview,
    }
    get_pubsub().publish(topic_session(session_id), payload)
