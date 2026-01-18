"""Ship Zerg run events to Life Hub (best-effort)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from zerg.config import get_settings
from zerg.database import db_session
from zerg.services.event_store import EventStore

logger = logging.getLogger(__name__)

SKIP_EVENT_TYPES = {
    "supervisor_thinking",
    "supervisor_token",
    "run_updated",
    "supervisor_tool_progress",
    "supervisor_heartbeat",
    "worker_heartbeat",
}


async def ship_run_to_lifehub(run_id: int, trace_id: str | None) -> None:
    """Ship completed run events to Life Hub (fire-and-forget)."""
    settings = get_settings()
    if settings.testing or not settings.lifehub_shipping_enabled:
        return
    if not settings.lifehub_api_key:
        logger.debug("Life Hub shipping enabled but LIFE_HUB_API_KEY is missing")
        return

    try:
        with db_session() as db:
            events = EventStore.get_events_after(db, run_id, include_tokens=False)

        formatted = []
        for event in events:
            if event.event_type in SKIP_EVENT_TYPES:
                continue
            timestamp = event.created_at.isoformat() if event.created_at else None
            raw_obj = {
                "event_type": event.event_type,
                "payload": event.payload,
                "timestamp": timestamp,
            }
            formatted.append(
                {
                    "raw_text": json.dumps(raw_obj),
                    "timestamp": timestamp,
                }
            )

        if not formatted:
            return

        payload = {
            "device_id": "zerg-prod",
            "provider": "swarmlet",
            "source_path": f"zerg://runs/{run_id}",
            "provider_session_id": trace_id,
            "events": formatted,
        }

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.lifehub_url}/ingest/agents/events",
                json=payload,
                headers={"X-API-Key": settings.lifehub_api_key},
            )
            resp.raise_for_status()

        logger.info("Shipped run %s to Life Hub (%s events)", run_id, len(formatted))

    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to ship run %s to Life Hub: %s", run_id, exc)


def schedule_lifehub_shipping(run_id: int, trace_id: str | None) -> None:
    """Schedule Life Hub shipping without blocking the caller."""
    settings = get_settings()
    if settings.testing or not settings.lifehub_shipping_enabled:
        return
    if not settings.lifehub_api_key:
        logger.debug("Life Hub shipping enabled but LIFE_HUB_API_KEY is missing")
        return

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ship_run_to_lifehub(run_id, trace_id))
    except RuntimeError:
        logger.debug("No event loop; skipping Life Hub shipping for run %s", run_id)


__all__ = ["schedule_lifehub_shipping", "ship_run_to_lifehub"]
