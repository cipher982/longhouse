"""Email configuration API — status, override, test, and reset.

Lets users see whether email is configured (and from what source),
override platform defaults with custom SES credentials, test delivery,
and revert to platform defaults.

All email secrets are stored as JobSecret rows (owner_id=current_user.id)
with well-known keys (AWS_SES_ACCESS_KEY_ID, etc.).
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter
from fastapi import Depends
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import JobSecret
from zerg.models.models import User
from zerg.shared.email import _EMAIL_SECRET_KEYS
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system/email", tags=["email-config"])

# Keys that are SES credentials (sensitive)
_SES_CREDENTIAL_KEYS = {"AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY"}
# All configurable email keys
_ALL_EMAIL_KEYS = set(_EMAIL_SECRET_KEYS)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EmailKeyStatus(BaseModel):
    key: str
    configured: bool
    source: str | None = None  # "db", "env", or None


class EmailStatusResponse(BaseModel):
    configured: bool = Field(description="Whether email can send (SES creds + FROM_EMAIL present)")
    source: str | None = Field(None, description="Primary source: 'db', 'env', or None")
    keys: list[EmailKeyStatus]


class EmailConfigRequest(BaseModel):
    aws_ses_access_key_id: str | None = None
    aws_ses_secret_access_key: str | None = None
    aws_ses_region: str | None = None
    from_email: str | None = None
    notify_email: str | None = None
    digest_email: str | None = None
    alert_email: str | None = None


class EmailTestRequest(BaseModel):
    to_email: str | None = Field(None, description="Override recipient (defaults to NOTIFY_EMAIL or user email)")


class EmailTestResponse(BaseModel):
    success: bool
    message: str
    message_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_key_status(key: str, db: Session, owner_id: int) -> EmailKeyStatus:
    """Check a single email key: DB first, then env."""
    row = db.query(JobSecret).filter(JobSecret.owner_id == owner_id, JobSecret.key == key).first()
    if row:
        return EmailKeyStatus(key=key, configured=True, source="db")
    env_val = os.environ.get(key)
    if env_val:
        return EmailKeyStatus(key=key, configured=True, source="env")
    return EmailKeyStatus(key=key, configured=False, source=None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status", response_model=EmailStatusResponse)
def email_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EmailStatusResponse:
    """Show which email keys are configured and from what source."""
    keys = [_resolve_key_status(k, db, current_user.id) for k in _EMAIL_SECRET_KEYS]

    # Email is "configured" when we have SES creds + FROM_EMAIL
    key_map = {k.key: k for k in keys}
    has_creds = (
        key_map["AWS_SES_ACCESS_KEY_ID"].configured and key_map["AWS_SES_SECRET_ACCESS_KEY"].configured and key_map["FROM_EMAIL"].configured
    )

    # Primary source = source of the access key (most important credential)
    primary_source = key_map["AWS_SES_ACCESS_KEY_ID"].source

    return EmailStatusResponse(
        configured=has_creds,
        source=primary_source,
        keys=keys,
    )


@router.put("/config")
def save_email_config(
    request: EmailConfigRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Save email config as JobSecret rows (encrypted). Only saves non-None fields."""
    field_to_key = {
        "aws_ses_access_key_id": "AWS_SES_ACCESS_KEY_ID",
        "aws_ses_secret_access_key": "AWS_SES_SECRET_ACCESS_KEY",
        "aws_ses_region": "AWS_SES_REGION",
        "from_email": "FROM_EMAIL",
        "notify_email": "NOTIFY_EMAIL",
        "digest_email": "DIGEST_EMAIL",
        "alert_email": "ALERT_EMAIL",
    }

    saved = 0
    for field_name, secret_key in field_to_key.items():
        value = getattr(request, field_name)
        if value is None:
            continue

        encrypted = encrypt(value)
        existing = db.query(JobSecret).filter(JobSecret.owner_id == current_user.id, JobSecret.key == secret_key).first()

        if existing:
            existing.encrypted_value = encrypted
            existing.description = "Email config (user override)"
        else:
            db.add(
                JobSecret(
                    owner_id=current_user.id,
                    key=secret_key,
                    encrypted_value=encrypted,
                    description="Email config (user override)",
                )
            )
        saved += 1

    db.commit()
    logger.info("Saved %d email config keys for user %d", saved, current_user.id)
    return {"success": True, "keys_saved": saved}


@router.post("/test", response_model=EmailTestResponse)
def test_email(
    request: EmailTestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EmailTestResponse:
    """Send a test email using current config (DB + env resolved)."""
    from zerg.shared.email import resolve_email_config
    from zerg.shared.email import send_email

    config = resolve_email_config()

    if not config.get("AWS_SES_ACCESS_KEY_ID") or not config.get("AWS_SES_SECRET_ACCESS_KEY"):
        return EmailTestResponse(success=False, message="SES credentials not configured")

    if not config.get("FROM_EMAIL"):
        return EmailTestResponse(success=False, message="FROM_EMAIL not configured")

    recipient = request.to_email or config.get("NOTIFY_EMAIL") or current_user.email
    if not recipient:
        return EmailTestResponse(success=False, message="No recipient email available")

    message_id = send_email(
        subject="Longhouse Email Test",
        body="This is a test email from your Longhouse instance.\n\nIf you received this, email is working correctly.",
        to_email=recipient,
        job_id="email-test",
    )

    if message_id:
        return EmailTestResponse(
            success=True,
            message=f"Test email sent to {recipient}",
            message_id=message_id,
        )
    return EmailTestResponse(success=False, message="Failed to send — check SES credentials and FROM_EMAIL domain verification")


@router.delete("/config", status_code=status.HTTP_200_OK)
def delete_email_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Remove all email config overrides (revert to platform/env defaults)."""
    deleted = (
        db.query(JobSecret)
        .filter(
            JobSecret.owner_id == current_user.id,
            JobSecret.key.in_(list(_ALL_EMAIL_KEYS)),
        )
        .delete(synchronize_session="fetch")
    )
    db.commit()
    logger.info("Deleted %d email config keys for user %d", deleted, current_user.id)
    return {"success": True, "keys_deleted": deleted}
