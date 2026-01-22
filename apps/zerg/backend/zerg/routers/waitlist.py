"""Waitlist API endpoints.

Public endpoints for collecting email signups for features not yet available.
No authentication required.
"""

import logging
import re

import httpx
from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Depends
from pydantic import BaseModel
from pydantic import field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.database import get_db
from zerg.models import WaitlistEntry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/waitlist", tags=["waitlist"])

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class WaitlistRequest(BaseModel):
    """Request body for waitlist signup."""

    email: str
    source: str = "pricing_pro"
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


def _send_discord_alert_sync(email: str, source: str, count: int) -> None:
    """Send Discord alert synchronously (for background tasks)."""
    settings = get_settings()
    webhook_url = settings.discord_webhook_url
    alerts_enabled = settings.discord_enable_alerts

    if settings.testing or not alerts_enabled or not webhook_url:
        return

    content = f"📋 **Waitlist Signup!** {email} joined the {source} waitlist (#{count} on waitlist)"

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
    db: Session = Depends(get_db),
) -> WaitlistResponse:
    """Add email to waitlist.

    This endpoint is public (no auth required) since we want to collect
    signups from visitors who haven't signed up yet.
    """
    entry = WaitlistEntry(
        email=request.email.lower(),
        source=request.source,
        notes=request.notes,
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # Email already exists
        db.rollback()
        return WaitlistResponse(
            success=True,
            message="You're already on the waitlist! We'll notify you when Pro launches.",
        )

    # Get total count for Discord alert
    total_count = db.query(func.count(WaitlistEntry.id)).scalar()

    # Send Discord notification in background
    background_tasks.add_task(_send_discord_alert_sync, request.email.lower(), request.source, total_count)

    return WaitlistResponse(
        success=True,
        message="Thanks for joining! We'll notify you when Pro launches.",
    )
