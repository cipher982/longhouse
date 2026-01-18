"""Ship Zerg run events to Life Hub (best-effort)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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

# Keys in event payloads that should be redacted before shipping
REDACT_PAYLOAD_KEYS = {
    "api_key",
    "password",
    "token",
    "secret",
    "authorization",
    "auth",
    "credential",
    "credentials",
    "access_token",
    "refresh_token",
    "private_key",
}


def _redact_sensitive_fields(obj: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive fields from a dict/list structure.

    Args:
        obj: The object to redact (dict, list, or primitive).
        depth: Current recursion depth (to prevent infinite recursion).

    Returns:
        A copy of the object with sensitive fields redacted.
    """
    if depth > 10:  # Prevent infinite recursion
        return obj

    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            key_lower = key.lower()
            if any(sensitive in key_lower for sensitive in REDACT_PAYLOAD_KEYS):
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_sensitive_fields(value, depth + 1)
        return result
    elif isinstance(obj, list):
        return [_redact_sensitive_fields(item, depth + 1) for item in obj]
    else:
        return obj


# Track shipped run IDs to prevent duplicate shipping within the same process
_shipped_run_ids: set[int] = set()


async def ship_run_to_lifehub(run_id: int, trace_id: str | None) -> None:
    """Ship completed run events to Life Hub (fire-and-forget)."""
    settings = get_settings()
    if settings.testing or not settings.lifehub_shipping_enabled:
        return

    # Guard: skip if URL is not configured (empty string or None)
    if not settings.lifehub_url:
        logger.warning("Skipping Life Hub shipping: LIFE_HUB_URL not configured")
        return

    # Deduplication: skip if already shipped in this process
    if run_id in _shipped_run_ids:
        logger.debug("Skipping duplicate shipping for run %s", run_id)
        return

    try:
        with db_session() as db:
            events = EventStore.get_events_after(db, run_id, include_tokens=False)

        formatted = []
        for event in events:
            if event.event_type in SKIP_EVENT_TYPES:
                continue
            timestamp = event.created_at.isoformat() if event.created_at else None
            # Redact sensitive fields from payload before shipping
            redacted_payload = _redact_sensitive_fields(event.payload)
            raw_obj = {
                "event_type": event.event_type,
                "payload": redacted_payload,
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

        # Use environment-aware device_id
        env = settings.environment or "unknown"
        # Normalize environment string (e.g., "production" -> "prod", "development" -> "dev")
        if env in ("production", "prod"):
            device_id = "zerg-prod"
        elif env in ("development", "dev"):
            device_id = "zerg-dev"
        else:
            device_id = f"zerg-{env}"

        payload = {
            "device_id": device_id,
            "provider": "swarmlet",
            "source_path": f"zerg://runs/{run_id}",
            "provider_session_id": trace_id,
            "events": formatted,
        }

        headers = {}
        if settings.lifehub_api_key:
            headers["X-API-Key"] = settings.lifehub_api_key

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.lifehub_url}/ingest/agents/events",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()

        # Mark as shipped to prevent duplicates
        _shipped_run_ids.add(run_id)
        logger.info("Shipped run %s to Life Hub (%s events, device=%s)", run_id, len(formatted), device_id)

    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to ship run %s to Life Hub: %s", run_id, exc)


def schedule_lifehub_shipping(run_id: int, trace_id: str | None) -> None:
    """Schedule Life Hub shipping without blocking the caller."""
    settings = get_settings()
    if settings.testing or not settings.lifehub_shipping_enabled:
        return

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ship_run_to_lifehub(run_id, trace_id))
    except RuntimeError:
        logger.debug("No event loop; skipping Life Hub shipping for run %s", run_id)


__all__ = ["schedule_lifehub_shipping", "ship_run_to_lifehub"]
