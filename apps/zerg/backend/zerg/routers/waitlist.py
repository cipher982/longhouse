"""Waitlist API endpoints.

Public endpoints for collecting email signups for features not yet available.
No authentication required. Signups go to Discord; no local DB dependency.
"""

import logging
import re

import httpx
from fastapi import APIRouter
from fastapi import BackgroundTasks
from pydantic import BaseModel
from pydantic import field_validator

from zerg.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/waitlist", tags=["waitlist"])

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class WaitlistRequest(BaseModel):
    """Request body for waitlist signup."""

    email: str
    source: str = "pricing_hosted"
    notes: str | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email address")
        return v


class WaitlistResponse(BaseModel):
    """Response for waitlist signup."""

    success: bool
    message: str


def _send_discord_notification(email: str, source: str) -> None:
    """Send waitlist signup to Discord (the durable record)."""
    settings = get_settings()
    webhook_url = settings.discord_webhook_url

    if settings.testing or not webhook_url:
        logger.info("Waitlist signup (no webhook): %s from %s", email, source)
        return

    content = f"📋 **Waitlist Signup!** {email} (source: {source})"

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(webhook_url, json={"content": content})
            if resp.status_code >= 300:
                logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Discord webhook error: %s", exc)


@router.post("", response_model=WaitlistResponse)
def join_waitlist(
    request: WaitlistRequest,
    background_tasks: BackgroundTasks,
) -> WaitlistResponse:
    """Add email to waitlist via Discord notification.

    This endpoint is public (no auth required) since we want to collect
    signups from visitors who haven't signed up yet.
    """
    background_tasks.add_task(_send_discord_notification, request.email, request.source)

    return WaitlistResponse(
        success=True,
        message="Thanks for joining! We'll notify you when hosted launches.",
    )
