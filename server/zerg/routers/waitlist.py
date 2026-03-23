"""Waitlist API endpoints.

Public endpoints for collecting email signups for features not yet available.
No authentication required. Signups go to Discord; no local DB dependency.

Discord is the durable record — if the webhook is not configured or fails,
the endpoint returns an error so the user knows their signup wasn't recorded.
"""

import logging
import re

import httpx
from fastapi import APIRouter
from fastapi import Response
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


@router.post("", response_model=WaitlistResponse)
def join_waitlist(request: WaitlistRequest, response: Response) -> WaitlistResponse:
    """Add email to waitlist via Discord webhook.

    This endpoint is public (no auth required). Discord is the durable
    record — the call is synchronous so we know if it succeeded before
    telling the user.
    """
    settings = get_settings()
    webhook_url = settings.discord_webhook_url

    if settings.testing:
        logger.info("Waitlist signup (test mode): %s from %s", request.email, request.source)
        return WaitlistResponse(
            success=True,
            message="Thanks for joining! We'll notify you when hosted launches.",
        )

    if not webhook_url:
        logger.error("Waitlist signup failed: DISCORD_WEBHOOK_URL not configured")
        response.status_code = 503
        return WaitlistResponse(
            success=False,
            message="Waitlist is temporarily unavailable. Please try again later.",
        )

    content = f"**Waitlist Signup** | {request.email} | source: {request.source}"

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(webhook_url, json={"content": content})
            if resp.status_code >= 300:
                logger.error("Waitlist Discord webhook returned %s: %s", resp.status_code, resp.text)
                response.status_code = 502
                return WaitlistResponse(
                    success=False,
                    message="Waitlist is temporarily unavailable. Please try again later.",
                )
    except Exception as exc:
        logger.error("Waitlist Discord webhook error: %s", exc)
        response.status_code = 502
        return WaitlistResponse(
            success=False,
            message="Waitlist is temporarily unavailable. Please try again later.",
        )

    return WaitlistResponse(
        success=True,
        message="Thanks for joining! We'll notify you when hosted launches.",
    )
