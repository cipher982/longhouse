"""Gmail sync job - triggers incremental sync in Life-Hub.

Also handles Pub/Sub watch renewal to ensure real-time notifications stay active.
Migrated from Sauron.
"""

from __future__ import annotations

import logging
import os

import httpx

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry

logger = logging.getLogger(__name__)


async def _maybe_renew_watch(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
) -> dict | None:
    """Check and renew Gmail Pub/Sub watch if needed.

    Returns watch status dict or None if Pub/Sub is disabled.
    """
    watch_url = f"{base_url}/api/emails/pubsub/watch"

    try:
        # First check if Pub/Sub is even enabled
        status_url = f"{base_url}/api/emails/pubsub/status"
        status_resp = await client.get(status_url, headers=headers)

        if status_resp.status_code == 503:
            logger.debug("Pub/Sub not enabled, skipping watch renewal")
            return None

        status_resp.raise_for_status()
        status = status_resp.json()

        if not status.get("enabled"):
            logger.debug("Pub/Sub not enabled, skipping watch renewal")
            return None

        # Call watch endpoint - it handles renewal logic internally
        # renew_threshold_hours=24 means renew if expiring in <24 hours
        watch_resp = await client.post(
            watch_url,
            params={"renew_threshold_hours": 24},
            headers=headers,
        )
        watch_resp.raise_for_status()
        watch_result = watch_resp.json()

        if watch_result.get("renewed"):
            logger.info(
                "Gmail watch renewed (reason: %s), expires: %s",
                watch_result.get("reason"),
                watch_result.get("watch_expiry"),
            )
        else:
            hours_remaining = watch_result.get("hours_remaining", "?")
            logger.debug("Gmail watch active, %sh remaining", hours_remaining)

        return watch_result

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 503:
            logger.debug("Watch endpoint returned 503: %s", e.response.text)
            return None
        logger.warning("Watch renewal failed: %s", e)
        return None
    except Exception as e:
        logger.warning("Watch renewal check failed: %s", e)
        return None


async def run() -> dict:
    """Trigger Gmail sync and ensure Pub/Sub watch is active."""
    # Life-Hub URL - use external HTTPS URL since different Docker networks
    base_url = os.getenv("LIFE_HUB_API_URL", "https://data.drose.io")
    sync_url = f"{base_url}/api/emails/sync"

    api_key = os.getenv("LIFE_HUB_API_KEY")
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Check and renew Pub/Sub watch if needed
        watch_result = await _maybe_renew_watch(client, base_url, headers)

        # Step 2: Trigger the actual sync
        logger.info("Triggering Gmail sync via %s", sync_url)

        # days_back=7 for safety net backfill (if incremental fails)
        # sync_attachments=true ensures new emails get their attachments synced
        response = await client.post(
            sync_url,
            params={"days_back": 7, "sync_attachments": "true"},
            headers=headers,
        )
        response.raise_for_status()
        result = response.json()

    summary = result.get("message", "Sync triggered successfully")
    logger.info("Gmail sync result: %s", summary)

    # Include watch status in result for visibility
    if watch_result:
        result["watch_renewed"] = watch_result.get("renewed", False)
        result["watch_hours_remaining"] = watch_result.get("hours_remaining")

    return result


# Register job - runs every 30 minutes as backup to Pub/Sub
job_registry.register(
    JobConfig(
        id="gmail-sync",
        cron="*/30 * * * *",  # Every 30 minutes
        func=run,
        timeout_seconds=60,
        tags=["life-hub", "emails", "sync"],
        project="life-hub",
        description="Trigger Gmail sync and renew Pub/Sub watch",
    )
)
