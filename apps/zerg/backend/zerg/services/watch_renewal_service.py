"""Connector-level Gmail watch renewal service.

This service manages the renewal of Gmail watch subscriptions at the connector
level. Gmail watches expire after 7 days and need to be renewed before expiry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import db_session
from zerg.metrics import gmail_connector_watch_expiry
from zerg.metrics import gmail_watch_renew_total
from zerg.models.models import Connector
from zerg.services import gmail_api
from zerg.utils import crypto

logger = logging.getLogger(__name__)


class WatchRenewalService:
    """Service to manage Gmail watch renewals for connectors."""

    def __init__(self):
        """Initialize the watch renewal service."""
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_interval = 3600  # Check every hour

    async def start(self) -> None:
        """Start the watch renewal service."""
        if self._running:
            logger.warning("Watch renewal service already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._renewal_loop())
        logger.info("Watch renewal service started")

    async def stop(self) -> None:
        """Stop the watch renewal service."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Watch renewal service stopped")

    async def _renewal_loop(self) -> None:
        """Main loop that checks and renews expiring watches."""
        while self._running:
            try:
                await self._check_and_renew_watches()
            except Exception as e:
                logger.exception("Error in watch renewal loop", exc_info=e)

            # Wait before next check
            await asyncio.sleep(self._check_interval)

    async def _check_and_renew_watches(self) -> None:
        """Check all Gmail connectors and renew expiring watches."""
        with db_session() as session:
            # Find all Gmail connectors
            connectors = (
                session.query(Connector)
                .filter(
                    Connector.type == "email",
                    Connector.provider == "gmail",
                )
                .all()
            )

            now = time.time()
            renewal_threshold = now + (24 * 3600)  # Renew if expiring in 24 hours

            for conn in connectors:
                try:
                    await self._process_connector_renewal(session, conn, now, renewal_threshold)
                except Exception as e:
                    logger.error(
                        "Failed to process connector renewal",
                        extra={"connector_id": conn.id, "error": str(e)},
                    )

    async def _process_connector_renewal(
        self,
        session: Session,
        connector: Connector,
        now: float,
        renewal_threshold: float,
    ) -> None:
        """Process watch renewal for a single connector."""
        config = connector.config or {}
        watch_expiry = config.get("watch_expiry")

        # Update metrics
        if watch_expiry:
            gmail_connector_watch_expiry.labels(
                connector_id=str(connector.id),
                owner_id=str(connector.owner_id),
            ).set(watch_expiry / 1000)  # Convert to seconds

        # Skip if no watch or not expiring soon
        if not watch_expiry:
            logger.debug(
                "Connector has no watch to renew",
                extra={"connector_id": connector.id},
            )
            return

        # Gmail returns expiry in milliseconds
        expiry_seconds = watch_expiry / 1000

        if expiry_seconds > renewal_threshold:
            # Not expiring soon
            return

        # Renew the watch
        logger.info(
            "Renewing expiring watch",
            extra={
                "connector_id": connector.id,
                "expiry": datetime.fromtimestamp(expiry_seconds, tz=timezone.utc).isoformat(),
            },
        )

        # Get refresh token
        enc_token = config.get("refresh_token")
        if not enc_token:
            logger.warning(
                "No refresh token for connector",
                extra={"connector_id": connector.id},
            )
            return

        refresh_token = crypto.decrypt(enc_token)

        try:
            # Exchange for access token
            access_token = await gmail_api.async_exchange_refresh_token(refresh_token)

            # Determine callback URL
            from zerg.config import get_settings

            settings = get_settings()
            topic = getattr(settings, "gmail_pubsub_topic", None)
            # Renew the watch (use sync version in async context); prefer Pub/Sub
            if topic:
                watch_info = gmail_api.start_watch(
                    access_token=access_token,
                    topic_name=topic,
                )
            else:
                # Legacy/local fallback (not valid for real Gmail push, kept for tests)
                callback_url = (
                    f"{settings.app_public_url}/api/email/webhook/google"
                    if settings.app_public_url
                    else "https://localhost/api/email/webhook/google"
                )
                watch_info = gmail_api.start_watch(
                    access_token=access_token,
                    callback_url=callback_url,
                )

            # Update connector with new watch info
            config["history_id"] = watch_info["history_id"]
            config["watch_expiry"] = watch_info["watch_expiry"]

            crud.update_connector(session, connector.id, config=config)

            # Update metrics
            gmail_watch_renew_total.inc()
            gmail_connector_watch_expiry.labels(
                connector_id=str(connector.id),
                owner_id=str(connector.owner_id),
            ).set(watch_info["watch_expiry"] / 1000)

            logger.info(
                "Watch renewed successfully",
                extra={
                    "connector_id": connector.id,
                    "new_expiry": datetime.fromtimestamp(
                        watch_info["watch_expiry"] / 1000, tz=timezone.utc
                    ).isoformat(),
                },
            )

        except Exception as e:
            logger.error(
                "Failed to renew watch",
                extra={"connector_id": connector.id, "error": str(e)},
            )
            from zerg.metrics import gmail_api_error_total

            gmail_api_error_total.inc()


# Global instance
watch_renewal_service = WatchRenewalService()
