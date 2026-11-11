"""SPIKE (terminal-truth-baseline, track C): live terminal mirror relay.

One producer (a Machine Agent running a provider behind its PTY relay) sends
binary frames; any number of viewers (browsers) receive them. Nothing is
stored: no database rows, no byte history. A viewer that joins, or falls too
far behind, asks the producer for a snapshot of its emulator's screen and
then receives frames from that point on, so the host's memory is bounded by
the per-viewer queue regardless of how long the session has run.

Mounted only when LONGHOUSE_TERMINAL_MIRROR_SPIKE=1; no auth, so it must not
be enabled on a shared host. Wire format: see the module comment in
engine/src/terminal_mirror.rs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from dataclasses import field

from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/terminal-mirror", tags=["terminal-mirror-spike"])

KIND_SNAPSHOT = 3
KIND_INPUT = 16
KIND_RESIZE = 17
VIEWER_QUEUE_FRAMES = 2048


@dataclass(eq=False)
class Viewer:
    websocket: WebSocket
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=VIEWER_QUEUE_FRAMES))
    # False until a snapshot has been queued: frames before it are skipped.
    ready: bool = False
    resyncs: int = 0


@dataclass
class Session:
    producer: WebSocket | None = None
    viewers: set[Viewer] = field(default_factory=set)


_sessions: dict[str, Session] = {}


def _session(session_id: str) -> Session:
    return _sessions.setdefault(session_id, Session())


async def _request_snapshot(session: Session) -> None:
    if session.producer is not None:
        try:
            await session.producer.send_text("snapshot")
        except Exception:
            pass


@router.websocket("/{session_id}/ingest")
async def ingest(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    session = _session(session_id)
    session.producer = websocket
    # Viewers that arrived before the producer are waiting for a snapshot.
    if any(not viewer.ready for viewer in session.viewers):
        await _request_snapshot(session)
    for viewer in list(session.viewers):
        _status(viewer, producer=True)
    try:
        while True:
            frame = await websocket.receive_bytes()
            snapshot = bool(frame) and frame[0] == KIND_SNAPSHOT
            need_resync = False
            for viewer in list(session.viewers):
                if snapshot:
                    # A snapshot restarts every viewer that is not ready; a
                    # ready viewer already has the stream and skips it.
                    if viewer.ready:
                        continue
                    viewer.ready = True
                elif not viewer.ready:
                    continue
                try:
                    viewer.queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Too slow to keep up: drop what it has and resync it from
                    # a fresh snapshot instead of buffering without bound.
                    while not viewer.queue.empty():
                        viewer.queue.get_nowait()
                    viewer.ready = False
                    viewer.resyncs += 1
                    need_resync = True
            if need_resync:
                await _request_snapshot(session)
    except WebSocketDisconnect:
        pass
    finally:
        if session.producer is websocket:
            session.producer = None
        for viewer in list(session.viewers):
            viewer.ready = False
            _status(viewer, producer=False)


def _status(viewer: Viewer, *, producer: bool) -> None:
    try:
        viewer.queue.put_nowait(json.dumps({"type": "status", "producer": producer}))
    except asyncio.QueueFull:
        pass


@router.websocket("/{session_id}/view")
async def view(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    session = _session(session_id)
    viewer = Viewer(websocket)
    session.viewers.add(viewer)
    _status(viewer, producer=session.producer is not None)
    await _request_snapshot(session)

    async def pump() -> None:
        while True:
            item = await viewer.queue.get()
            if isinstance(item, str):
                await websocket.send_text(item)
            else:
                await websocket.send_bytes(item)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            data = await websocket.receive_bytes()
            # Keystrokes go to the producer, which writes them through the
            # relay's PTY write lock; a resize request sets the PTY size.
            if data and data[0] in (KIND_INPUT, KIND_RESIZE) and session.producer is not None:
                await session.producer.send_bytes(data)
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        session.viewers.discard(viewer)
