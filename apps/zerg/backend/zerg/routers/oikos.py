"""Oikos API router — assembles sub-routers and provides the /events SSE stream.

Sub-routers (each is a focused module with its own endpoints):
- oikos_chat: POST /chat, POST /run/{id}/cancel
- oikos_config: GET /bootstrap, PATCH /preferences, GET /thread, DELETE /thread, GET /session
- oikos_history: GET /history, DELETE /history (deprecated compatibility)
- oikos_fiches: GET /fiches
- oikos_runs: GET /runs, GET /runs/active, GET /runs/{id}, etc.
- oikos_internal: POST /internal/runs/{id}/resume
- voice: POST /voice/turn, POST /voice/transcribe, POST /voice/tts
"""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from sse_starlette.sse import EventSourceResponse

from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.routers import oikos_chat
from zerg.routers import oikos_config
from zerg.routers import oikos_conversations
from zerg.routers import oikos_fiches
from zerg.routers import oikos_history
from zerg.routers import oikos_internal
from zerg.routers import oikos_runs
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.voice import router as oikos_voice

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oikos", tags=["oikos"])

# Include sub-routers
router.include_router(oikos_chat.router)
router.include_router(oikos_config.router)
router.include_router(oikos_conversations.router)
router.include_router(oikos_history.router)
router.include_router(oikos_fiches.router)
router.include_router(oikos_runs.router)
router.include_router(oikos_internal.router)
router.include_router(oikos_voice.router, tags=["oikos-voice"])


# ---------------------------------------------------------------------------
# General SSE events stream (fiche/run updates for Task Inbox UI)
# ---------------------------------------------------------------------------


async def _event_generator(_current_user):
    queue: asyncio.Queue = asyncio.Queue()

    async def _handler(event):
        await queue.put(event)

    event_bus.subscribe(EventType.FICHE_UPDATED, _handler)
    event_bus.subscribe(EventType.RUN_CREATED, _handler)
    event_bus.subscribe(EventType.RUN_UPDATED, _handler)

    try:
        yield {"event": "connected", "data": json.dumps({"message": "Oikos SSE stream connected"})}

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                event_type = event.get("event_type") or event.get("type") or "event"
                payload = {k: v for k, v in event.items() if k not in {"event_type", "type"}}
                yield {
                    "event": event_type,
                    "data": json.dumps(
                        {
                            "type": event_type,
                            "payload": payload,
                            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }
                    ),
                }
            except asyncio.TimeoutError:
                ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                yield {"event": "heartbeat", "data": json.dumps({"timestamp": ts})}

    except asyncio.CancelledError:
        logger.info("Oikos SSE stream disconnected")
    finally:
        event_bus.unsubscribe(EventType.FICHE_UPDATED, _handler)
        event_bus.unsubscribe(EventType.RUN_CREATED, _handler)
        event_bus.unsubscribe(EventType.RUN_UPDATED, _handler)


@router.get("/events")
async def oikos_events(
    current_user=Depends(get_current_oikos_user),
) -> EventSourceResponse:
    """SSE stream for real-time fiche/run updates (Task Inbox UI)."""
    return EventSourceResponse(_event_generator(current_user))
