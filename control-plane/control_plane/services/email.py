"""Email sending via AWS SES for the control plane."""
from __future__ import annotations

import logging

import boto3

from control_plane.config import settings

logger = logging.getLogger(__name__)


def _get_ses_client():
    """Create a boto3 SES client using instance SES credentials."""
    if not settings.instance_aws_ses_access_key_id or not settings.instance_aws_ses_secret_access_key:
        raise RuntimeError("SES credentials not configured (CONTROL_PLANE_INSTANCE_AWS_SES_*)")
    return boto3.client(
        "ses",
        region_name=settings.instance_aws_ses_region or "us-east-1",
        aws_access_key_id=settings.instance_aws_ses_access_key_id,
        aws_secret_access_key=settings.instance_aws_ses_secret_access_key,
    )


def send_verification_email(to: str, verify_url: str) -> None:
    """Send an email verification link to the user."""
    # Always send control plane emails from longhouse.ai, not the instance sender
    # (instance_from_email may be drose.io which causes spoofing issues with Google Workspace)
    from_email = "noreply@longhouse.ai"
    subject = "Verify your Longhouse email"
    html_body = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 480px; margin: 0 auto; padding: 2rem;">
  <h2 style="color: #1a1a2e;">Verify your email</h2>
  <p style="color: #555; line-height: 1.6;">
    Click the button below to verify your email and get started with Longhouse.
  </p>
  <a href="{verify_url}"
     style="display: inline-block; padding: 12px 28px; background: #6366f1; color: #fff;
            text-decoration: none; border-radius: 8px; font-weight: 500; margin: 1rem 0;">
    Verify Email
  </a>
  <p style="color: #999; font-size: 0.85rem; margin-top: 1.5rem;">
    If the button doesn't work, copy this link:<br>
    <a href="{verify_url}" style="color: #6366f1; word-break: break-all;">{verify_url}</a>
  </p>
  <p style="color: #999; font-size: 0.8rem;">This link expires in 24 hours.</p>
</div>"""

    text_body = f"Verify your Longhouse email:\n\n{verify_url}\n\nThis link expires in 24 hours."

    try:
        client = _get_ses_client()
        client.send_email(
            Source=from_email,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
        logger.info(f"Sent verification email to {to}")
    except Exception:
        logger.exception(f"Failed to send verification email to {to}")
        raise


def send_password_reset_email(to: str, reset_url: str) -> None:
    """Send a password reset link to the user."""
    from_email = "noreply@longhouse.ai"
    subject = "Reset your Longhouse password"
    html_body = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 480px; margin: 0 auto; padding: 2rem;">
  <h2 style="color: #1a1a2e;">Reset your password</h2>
  <p style="color: #555; line-height: 1.6;">
    We received a request to reset your Longhouse password. Click the button below to choose a new one.
  </p>
  <a href="{reset_url}"
     style="display: inline-block; padding: 12px 28px; background: #6366f1; color: #fff;
            text-decoration: none; border-radius: 8px; font-weight: 500; margin: 1rem 0;">
    Reset Password
  </a>
  <p style="color: #999; font-size: 0.85rem; margin-top: 1.5rem;">
    If the button doesn't work, copy this link:<br>
    <a href="{reset_url}" style="color: #6366f1; word-break: break-all;">{reset_url}</a>
  </p>
  <p style="color: #999; font-size: 0.8rem;">This link expires in 1 hour. If you didn't request this, you can safely ignore this email.</p>
</div>"""

    text_body = f"""Reset your Longhouse password:

{reset_url}

This link expires in 1 hour. If you didn't request this, you can safely ignore this email."""

    try:
        client = _get_ses_client()
        client.send_email(
            Source=from_email,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
        logger.info(f"Sent password reset email to {to}")
    except Exception:
        logger.exception(f"Failed to send password reset email to {to}")
        raise
