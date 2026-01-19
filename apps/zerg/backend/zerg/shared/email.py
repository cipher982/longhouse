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
    aws_access_key_id = os.getenv("AWS_SES_ACCESS_KEY_ID")
    aws_secret_access_key = os.getenv("AWS_SES_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_SES_REGION", "us-east-1")
    from_email = os.getenv("FROM_EMAIL")
    notify_email = os.getenv("NOTIFY_EMAIL")

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
    digest_email = os.getenv("DIGEST_EMAIL")
    return send_email(
        f"ZERG DIGEST: {subject}",
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
    alert_email = os.getenv("ALERT_EMAIL")
    return send_email(
        f"{level} (ZERG): {subject}",
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
    body = f"""Zerg job failed:

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
    aws_access_key_id = os.getenv("AWS_SES_ACCESS_KEY_ID")
    aws_secret_access_key = os.getenv("AWS_SES_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_SES_REGION", "us-east-1")
    from_email = os.getenv("FROM_EMAIL")

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
