"""Waitlist API endpoints.

Public endpoints for collecting email signups for features not yet available.
No authentication required.
"""

import re

from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from pydantic import field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.models import WaitlistEntry

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


@router.post("", response_model=WaitlistResponse)
def join_waitlist(request: WaitlistRequest, db: Session = Depends(get_db)) -> WaitlistResponse:
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

    return WaitlistResponse(
        success=True,
        message="Thanks for joining! We'll notify you when Pro launches.",
    )
