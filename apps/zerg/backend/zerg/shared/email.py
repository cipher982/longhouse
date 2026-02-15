"""Email utilities using AWS SES.

Ported from Sauron for use in scheduled jobs.
"""

from __future__ import annotations

import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Well-known JobSecret keys for email configuration
_EMAIL_SECRET_KEYS = [
    "AWS_SES_ACCESS_KEY_ID",
    "AWS_SES_SECRET_ACCESS_KEY",
    "AWS_SES_REGION",
    "FROM_EMAIL",
    "NOTIFY_EMAIL",
    "DIGEST_EMAIL",
    "ALERT_EMAIL",
]


def get_single_tenant_owner_id() -> int:
    """Get the owner ID for single-tenant mode.

    Looks for the user who actually owns email secrets first (handles the case
    where secrets were saved by a non-first user). Falls back to the first user
    by ID, then to 1 if the DB is unavailable.
    """
    try:
        from zerg.database import get_session_factory
        from zerg.models.models import JobSecret
        from zerg.models.user import User

        session_factory = get_session_factory()
        with session_factory() as db:
            # Prefer the user who actually has email secrets
            secret_owner = db.query(JobSecret.owner_id).filter(JobSecret.key == "AWS_SES_ACCESS_KEY_ID").first()
            if secret_owner:
                return secret_owner[0]
            # Fall back to first user
            user = db.query(User).order_by(User.id).first()
            if user:
                return user.id
    except Exception as e:
        logger.debug("Could not determine tenant owner ID: %s", e)
    return 1


def resolve_email_config() -> dict[str, str]:
    """Resolve email config: DB (JobSecret) first, env var fallback.

    Checks the JobSecret table for the single-tenant owner first,
    then falls back to environment variables. This lets the control plane
    inject SES creds as env vars while allowing users to override via Settings UI.

    Returns:
        Dict mapping config key -> value for all resolved email keys.
    """
    resolved: dict[str, str] = {}

    # Try DB first (graceful — may fail if DB not ready or no secrets table)
    try:
        from zerg.database import get_session_factory
        from zerg.jobs.secret_resolver import resolve_secrets

        owner_id = get_single_tenant_owner_id()
        session_factory = get_session_factory()
        with session_factory() as db:
            resolved = resolve_secrets(owner_id=owner_id, declared_keys=_EMAIL_SECRET_KEYS, db=db)
    except Exception as e:
        # DB not available (e.g. during startup or tests) — fall through to env
        logger.debug("DB lookup failed for email config: %s", e)

    # Env var fallback for any keys not resolved from DB
    for key in _EMAIL_SECRET_KEYS:
        if key not in resolved:
            val = os.environ.get(key)
            if val:
                resolved[key] = val

    return resolved


def _get_ses_client(*, region: str, access_key_id: str, secret_access_key: str):
    """Get boto3 SES client."""
    return boto3.client(
        "ses",
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )


def send_email(
    subject: str,
    body: str,
    *,
    to_email: str | None = None,
    html: str | None = None,
    alert_type: str = "general",
    job_id: str = "unknown",
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """
    Send email via AWS SES.

    Args:
        subject: Email subject line
        body: Plain text body
        to_email: Recipient email
        html: Optional HTML body
        alert_type: Type for categorization (default: "general")
        job_id: Job that sent this email (default: "unknown")
        metadata: Optional context metadata

    Returns:
        SES Message-ID if sent successfully, None otherwise.
        Format: "01000..." (raw SES ID, not the full header format)
    """
    config = resolve_email_config()
    aws_access_key_id = config.get("AWS_SES_ACCESS_KEY_ID")
    aws_secret_access_key = config.get("AWS_SES_SECRET_ACCESS_KEY")
    aws_region = config.get("AWS_SES_REGION", "us-east-1")
    from_email = config.get("FROM_EMAIL")
    notify_email = config.get("NOTIFY_EMAIL")

    if not aws_access_key_id or not aws_secret_access_key:
        logger.error("AWS SES credentials not configured")
        return None

    if not from_email:
        logger.error("FROM_EMAIL not configured")
        return None

    recipient = to_email or notify_email
    if not recipient:
        logger.error("No recipient email configured")
        return None

    try:
        client = _get_ses_client(
            region=aws_region,
            access_key_id=aws_access_key_id,
            secret_access_key=aws_secret_access_key,
        )

        body_content = {"Text": {"Data": body, "Charset": "UTF-8"}}
        if html:
            body_content["Html"] = {"Data": html, "Charset": "UTF-8"}

        response = client.send_email(
            Source=from_email,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": body_content,
            },
        )
        message_id = response.get("MessageId")
        logger.info("Email sent: %s (job=%s)", message_id, job_id)

        return message_id

    except ClientError as e:
        logger.exception("Failed to send email: %s", e)
        return None


def send_digest_email(
    subject: str,
    body: str,
    html: str | None = None,
    *,
    alert_type: str = "digest",
    job_id: str = "unknown",
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Send a routine digest email."""
    config = resolve_email_config()
    digest_email = config.get("DIGEST_EMAIL")
    return send_email(
        f"LONGHOUSE DIGEST: {subject}",
        body,
        to_email=digest_email,
        html=html,
        alert_type=alert_type,
        job_id=job_id,
        metadata=metadata,
    )


def send_alert_email(
    subject: str,
    body: str,
    *,
    level: str = "WARNING",
    html: str | None = None,
    alert_type: str = "alert",
    job_id: str = "unknown",
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Send an alert email.

    Returns:
        SES Message-ID if sent successfully, None otherwise.
    """
    level = (level or "WARNING").upper()
    config = resolve_email_config()
    alert_email = config.get("ALERT_EMAIL")
    return send_email(
        f"{level} (LONGHOUSE): {subject}",
        body,
        to_email=alert_email,
        html=html,
        alert_type=alert_type,
        job_id=job_id,
        metadata=metadata,
    )


def send_job_failure_alert(failed_job_id: str, error: str) -> str | None:
    """Send alert for job failure."""
    subject = f"Job failed: {failed_job_id}"
    body = f"""Longhouse job failed:

Job ID: {failed_job_id}
Error: {error}

Check logs: ssh zerg "docker logs --tail 100 $(docker ps -qf name=zerg)"
"""
    return send_alert_email(
        subject,
        body,
        level="WARNING",
        alert_type="job_failure",
        job_id=failed_job_id,
        metadata={"error": error[:500]},
    )


def send_reply_email(
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: str,
    *,
    references: str | None = None,
) -> str | None:
    """
    Send a reply email with proper threading headers.

    Uses SES raw email API to set In-Reply-To and References headers
    for proper email client threading.

    Args:
        to_email: Recipient email address
        subject: Email subject (should include "Re: " prefix)
        body: Plain text body
        in_reply_to: Message-ID of the email being replied to
        references: Optional References header (chain of Message-IDs)

    Returns:
        SES Message-ID if sent successfully, None otherwise.
    """
    config = resolve_email_config()
    aws_access_key_id = config.get("AWS_SES_ACCESS_KEY_ID")
    aws_secret_access_key = config.get("AWS_SES_SECRET_ACCESS_KEY")
    aws_region = config.get("AWS_SES_REGION", "us-east-1")
    from_email = config.get("FROM_EMAIL")

    if not aws_access_key_id or not aws_secret_access_key:
        logger.error("AWS SES credentials not configured")
        return None

    if not from_email:
        logger.error("FROM_EMAIL not configured")
        return None

    try:
        client = _get_ses_client(
            region=aws_region,
            access_key_id=aws_access_key_id,
            secret_access_key=aws_secret_access_key,
        )

        # Build MIME message with threading headers
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to

        # Add plain text body
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Send raw email
        response = client.send_raw_email(
            Source=from_email,
            Destinations=[to_email],
            RawMessage={"Data": msg.as_string()},
        )
        message_id = response.get("MessageId")
        logger.info("Reply email sent: %s (in-reply-to: %s)", message_id, in_reply_to)
        return message_id

    except ClientError as e:
        logger.exception("Failed to send reply email: %s", e)
        return None
