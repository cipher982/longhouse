"""Email utilities for Longhouse instance mail.

Sends via AWS SES. Used by the assistant email tools, runner-health alerts, and
the email-config Settings surface. SES credentials resolve from the
``email_secrets`` table first, then fall back to environment variables.
"""

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Active email config keys. Longhouse uses one recipient for all instance mail.
_EMAIL_SECRET_KEYS = [
    "AWS_SES_ACCESS_KEY_ID",
    "AWS_SES_SECRET_ACCESS_KEY",
    "AWS_SES_REGION",
    "FROM_EMAIL",
    "NOTIFY_EMAIL",
]


def get_single_tenant_owner_id() -> int:
    """Get the owner ID for single-tenant mode.

    Looks for the user who actually owns email secrets first (handles the case
    where secrets were saved by a non-first user). Falls back to the first user
    by ID, then to 1 if the DB is unavailable.
    """
    try:
        from zerg.database import get_session_factory
        from zerg.models.models import EmailSecret
        from zerg.models.user import User

        session_factory = get_session_factory()
        with session_factory() as db:
            # Prefer the user who actually has email secrets
            secret_owner = db.query(EmailSecret.owner_id).filter(EmailSecret.key == "AWS_SES_ACCESS_KEY_ID").first()
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
    """Resolve email config: DB (email_secrets) first, env var fallback.

    Checks the email_secrets table for the single-tenant owner first,
    then falls back to environment variables. This lets the control plane
    inject SES creds as env vars while allowing users to override via Settings UI.

    Returns:
        Dict mapping config key -> value for all resolved email keys.
    """
    resolved: dict[str, str] = {}

    # Try DB first (graceful — may fail if DB not ready or no secrets table)
    try:
        from zerg.database import get_session_factory
        from zerg.models.models import EmailSecret
        from zerg.utils.crypto import decrypt

        owner_id = get_single_tenant_owner_id()
        session_factory = get_session_factory()
        with session_factory() as db:
            rows = db.query(EmailSecret).filter(EmailSecret.owner_id == owner_id, EmailSecret.key.in_(_EMAIL_SECRET_KEYS)).all()
            for row in rows:
                try:
                    resolved[row.key] = decrypt(row.encrypted_value)
                except Exception:
                    logger.warning("Failed to decrypt email secret %s", row.key)
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
) -> str | None:
    """
    Send email via AWS SES.

    Args:
        subject: Email subject line
        body: Plain text body
        to_email: Recipient email
        html: Optional HTML body
        alert_type: Category label for logging (default: "general")

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
        logger.info("Email sent: %s (type=%s)", message_id, alert_type)

        return message_id

    except ClientError as e:
        logger.exception("Failed to send email: %s", e)
        return None
