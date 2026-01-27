"""Email tool for sending emails.

Supports platform-level email (zero config) and optional user credentials for custom sender.
Includes approved contacts validation and rate limiting.
"""

import logging
import os
import re
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

import boto3
from botocore.exceptions import ClientError
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.context import get_commis_context
from zerg.database import db_session
from zerg.models.models import EmailSendLog
from zerg.models.models import User
from zerg.models.models import UserDailyEmailCounter
from zerg.models.models import UserEmailContact
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)

# Default AWS region for SES
DEFAULT_AWS_REGION = "us-east-1"

# Default daily email limit
DEFAULT_DAILY_EMAIL_LIMIT = 20


def _get_user_id() -> int | None:
    """Get user_id from context.

    Try multiple sources:
    1. Commis context (for background commis)
    2. Credential resolver (for fiche execution)

    Returns:
        User ID if found, None otherwise
    """
    # Try commis context first
    commis_ctx = get_commis_context()
    if commis_ctx and commis_ctx.owner_id:
        return commis_ctx.owner_id

    # Try credential resolver
    resolver = get_credential_resolver()
    if resolver and resolver.owner_id:
        return resolver.owner_id

    return None


def _get_platform_email_credentials() -> Dict[str, str] | None:
    """Get platform-level email credentials from environment.

    Returns:
        Dict with credentials if configured, None otherwise.
    """
    access_key = os.getenv("AWS_SES_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SES_SECRET_ACCESS_KEY")
    from_email = os.getenv("FROM_EMAIL")

    if access_key and secret_key and from_email:
        return {
            "access_key_id": access_key,
            "secret_access_key": secret_key,
            "region": os.getenv("AWS_SES_REGION", DEFAULT_AWS_REGION),
            "from_email": from_email,
        }
    return None


def _validate_email(email: str) -> bool:
    """Validate email address format.

    Args:
        email: Email address to validate

    Returns:
        True if valid, False otherwise
    """
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def _normalize_email(email: str) -> str:
    """Normalize email for comparison. Returns lowercase, trimmed.

    Strips display name if present: "Jane <jane@example.com>" -> "jane@example.com"
    """
    email = email.strip().lower()
    # Strip display name if present
    if "<" in email and ">" in email:
        email = email.split("<")[1].split(">")[0].strip().lower()
    return email


def _validate_no_header_injection(value: str, field_name: str) -> None:
    """Raise ValueError if value contains header injection characters."""
    if "\r" in value or "\n" in value:
        raise ValueError(f"Invalid characters in {field_name}")


def _validate_email_list(emails: Union[str, List[str]]) -> List[str]:
    """Validate and normalize email list.

    Args:
        emails: Single email or list of emails

    Returns:
        List of validated email addresses

    Raises:
        ValueError: If any email is invalid
    """
    if isinstance(emails, str):
        # Handle comma-separated
        emails = [e.strip() for e in emails.split(",") if e.strip()]

    validated = []
    for email in emails:
        email = email.strip()
        # Validate no header injection
        _validate_no_header_injection(email, "email address")
        # Normalize for format validation
        normalized = _normalize_email(email)
        if not _validate_email(normalized):
            raise ValueError(f"Invalid email address: {email}")
        validated.append(email)

    return validated


def _get_all_recipients(
    to: Union[str, List[str], None],
    cc: Union[str, List[str], None],
    bcc: Union[str, List[str], None],
) -> List[str]:
    """Extract and normalize all recipient emails from to/cc/bcc.

    Returns:
        List of normalized email addresses
    """
    recipients = []
    for field in [to, cc, bcc]:
        if not field:
            continue
        if isinstance(field, str):
            # Handle comma-separated
            recipients.extend([_normalize_email(e) for e in field.split(",") if e.strip()])
        elif isinstance(field, list):
            recipients.extend([_normalize_email(e) for e in field])
    return [r for r in recipients if r]  # Filter empty


def _is_approved_recipient(user_id: int, email: str, db) -> bool:
    """Check if normalized email is user's own or in approved contacts.

    Args:
        user_id: User ID to check contacts for
        email: Normalized email address to check
        db: Database session

    Returns:
        True if approved, False otherwise
    """
    normalized = _normalize_email(email)

    # Check user's own email
    user = db.query(User).filter(User.id == user_id).first()
    if user and _normalize_email(user.email) == normalized:
        return True

    # Check approved contacts (stored normalized)
    contact = (
        db.query(UserEmailContact)
        .filter(
            UserEmailContact.owner_id == user_id,
            UserEmailContact.email_normalized == normalized,
        )
        .first()
    )
    return contact is not None


def _reserve_rate_limit(user_id: int, recipient_count: int, db) -> tuple[bool, str]:
    """Atomically check and reserve rate limit BEFORE sending.

    Uses SELECT FOR UPDATE to prevent race conditions.
    Uses INSERT ON CONFLICT to handle concurrent counter creation.
    Returns (allowed, error_message).
    """
    from sqlalchemy.dialects.postgresql import insert

    today = datetime.now(timezone.utc).date()

    # Upsert counter to handle race condition on creation
    # This atomically creates the counter if it doesn't exist
    stmt = (
        insert(UserDailyEmailCounter)
        .values(
            user_id=user_id,
            date=today,
            count=0,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "date"])
    )
    db.execute(stmt)
    db.flush()

    # Now get with lock - guaranteed to exist
    counter = (
        db.query(UserDailyEmailCounter)
        .filter(
            UserDailyEmailCounter.user_id == user_id,
            UserDailyEmailCounter.date == today,
        )
        .with_for_update()
        .first()
    )

    limit = int(os.getenv("DAILY_EMAIL_LIMIT", str(DEFAULT_DAILY_EMAIL_LIMIT)))
    if counter.count + recipient_count > limit:
        return False, f"Daily email limit reached ({counter.count}/{limit}). Resets at midnight UTC."

    # Reserve the slots
    counter.count += recipient_count
    db.flush()  # Holds lock until commit
    return True, ""


def _log_email_sends(user_id: int, recipients: List[str], db) -> None:
    """Log email sends for audit purposes."""
    now = datetime.now(timezone.utc)
    for recipient in recipients:
        db.add(EmailSendLog(user_id=user_id, to_email=recipient, sent_at=now))


def send_email(
    to: Union[str, List[str]],
    subject: str,
    text: Optional[str] = None,
    html: Optional[str] = None,
    reply_to: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Send an email to one or more recipients.

    Recipients must be in your approved contacts list (Settings → Contacts)
    or be your own email address. Subject to daily rate limits.

    Args:
        to: Recipient email address(es) - string or list
        subject: Email subject line
        text: Plain text email content (optional if html provided)
        html: HTML email content (optional if text provided)
        reply_to: Reply-to email address (optional)
        cc: CC recipient email address(es) - string or list (optional)
        bcc: BCC recipient email address(es) - string or list (optional)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if email was sent
        - message_id: Unique message ID (if successful)
        - error: Error message (if failed)

    Example:
        >>> send_email(
        ...     to="user@example.com",
        ...     subject="Welcome!",
        ...     html="<h1>Welcome to our service!</h1>"
        ... )
        {"success": True, "message_id": "abc123..."}
    """
    try:
        # Get user context for contact validation and rate limiting
        user_id = _get_user_id()

        # Credential resolution order:
        # 1. User-configured credentials (account or fiche level)
        # 2. Platform-level credentials (from environment)
        resolved_access_key = None
        resolved_secret_key = None
        resolved_region = None
        resolved_from_email = None

        # Try user credentials first
        resolver = get_credential_resolver()
        if resolver:
            creds = resolver.get(ConnectorType.EMAIL)
            if creds:
                resolved_access_key = creds.get("access_key_id")
                resolved_secret_key = creds.get("secret_access_key")
                resolved_region = creds.get("region")
                resolved_from_email = creds.get("from_email")

        # Fall back to platform credentials
        if not resolved_access_key or not resolved_secret_key or not resolved_from_email:
            platform_creds = _get_platform_email_credentials()
            if platform_creds:
                resolved_access_key = resolved_access_key or platform_creds["access_key_id"]
                resolved_secret_key = resolved_secret_key or platform_creds["secret_access_key"]
                resolved_region = resolved_region or platform_creds["region"]
                resolved_from_email = resolved_from_email or platform_creds["from_email"]

        # Use default region if not specified
        resolved_region = resolved_region or DEFAULT_AWS_REGION

        # Validate we have credentials from somewhere
        if not resolved_access_key or not resolved_secret_key or not resolved_from_email:
            return tool_error(
                error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
                user_message="Email is not available. Please contact support.",
                connector="email",
            )

        if not to:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="to is required",
                connector="email",
            )

        if not subject:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="subject is required",
                connector="email",
            )

        if not text and not html:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Either text or html content is required",
                connector="email",
            )

        # Validate no header injection in subject
        try:
            _validate_no_header_injection(subject, "subject")
        except ValueError as e:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=str(e),
                connector="email",
            )

        # Validate from_email
        if not _validate_email(resolved_from_email):
            logger.error(f"Invalid from_email: {resolved_from_email}")
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid from_email: {resolved_from_email}",
                connector="email",
            )

        # Validate to addresses
        try:
            to_list = _validate_email_list(to)
        except ValueError as e:
            logger.error(f"Invalid to addresses: {e}")
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=str(e),
                connector="email",
            )

        # Build destination
        destination: Dict[str, Any] = {"ToAddresses": to_list}

        if cc:
            try:
                destination["CcAddresses"] = _validate_email_list(cc)
            except ValueError as e:
                logger.error(f"Invalid CC addresses: {e}")
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"CC validation: {str(e)}",
                    connector="email",
                )

        if bcc:
            try:
                destination["BccAddresses"] = _validate_email_list(bcc)
            except ValueError as e:
                logger.error(f"Invalid BCC addresses: {e}")
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"BCC validation: {str(e)}",
                    connector="email",
                )

        # Get ALL recipients for contact validation and rate limiting
        all_recipients = _get_all_recipients(to, cc, bcc)
        if not all_recipients:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="No valid recipients specified",
                connector="email",
            )

        # Contact validation and rate limiting (requires user context)
        # SECURITY: Fail closed - require user context for all sends
        if not user_id:
            logger.error("No user context for email send - rejecting for security")
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Unable to verify sender identity. Please try again.",
                connector="email",
            )

        # De-duplicate recipients for rate limiting (same email in to/cc/bcc counts once)
        unique_recipients = list(set(all_recipients))

        with db_session() as db:
            # Validate ALL recipients are approved
            for recipient in unique_recipients:
                if not _is_approved_recipient(user_id, recipient, db):
                    return tool_error(
                        error_type=ErrorType.VALIDATION_ERROR,
                        user_message=(
                            f"'{recipient}' is not in your approved contacts. " "Add them in Settings → Contacts before sending."
                        ),
                        connector="email",
                    )

            # Atomic rate limit check (counts unique recipients)
            allowed, msg = _reserve_rate_limit(user_id, len(unique_recipients), db)
            if not allowed:
                return tool_error(
                    error_type=ErrorType.RATE_LIMITED,
                    user_message=msg,
                    connector="email",
                )

            # Commit rate limit reservation before send attempt
            db.commit()

        # Build message body
        body: Dict[str, Any] = {}
        if text:
            body["Text"] = {"Data": text, "Charset": "UTF-8"}
        if html:
            body["Html"] = {"Data": html, "Charset": "UTF-8"}

        # Build message
        message = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": body,
        }

        # Create SES client
        client = boto3.client(
            "ses",
            region_name=resolved_region,
            aws_access_key_id=resolved_access_key,
            aws_secret_access_key=resolved_secret_key,
        )

        # Build send_email kwargs
        send_kwargs: Dict[str, Any] = {
            "Source": resolved_from_email,
            "Destination": destination,
            "Message": message,
        }

        if reply_to:
            if not _validate_email(reply_to):
                logger.error(f"Invalid reply_to: {reply_to}")
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"Invalid reply_to: {reply_to}",
                    connector="email",
                )
            send_kwargs["ReplyToAddresses"] = [reply_to]

        # Send email
        logger.info(f"Sending email from {resolved_from_email} to {to_list}")
        response = client.send_email(**send_kwargs)
        message_id = response.get("MessageId", "unknown")
        logger.info(f"Email sent successfully: {message_id}")

        # Log sends for audit AFTER successful send
        with db_session() as db:
            _log_email_sends(user_id, unique_recipients, db)
            db.commit()

        return tool_success({"message_id": message_id})

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"Email service error ({error_code}): {error_message}")

        # Map error codes to user-friendly messages
        if error_code in ("InvalidClientTokenId", "SignatureDoesNotMatch"):
            return tool_error(
                error_type=ErrorType.INVALID_CREDENTIALS,
                user_message="Email service configuration error. Please contact support.",
                connector="email",
            )
        elif error_code == "AccessDenied":
            return tool_error(
                error_type=ErrorType.PERMISSION_DENIED,
                user_message="Email service access denied. Please contact support.",
                connector="email",
            )
        elif error_code == "MessageRejected":
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Email rejected: {error_message}",
                connector="email",
            )
        elif error_code == "Throttling":
            return tool_error(
                error_type=ErrorType.RATE_LIMITED,
                user_message="Email rate limit reached. Please try again later.",
                connector="email",
            )
        else:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message=f"Failed to send email: {error_message}",
                connector="email",
            )

    except Exception as e:
        logger.exception("Unexpected error sending email")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to send email: {str(e)}",
            connector="email",
        )


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=send_email,
        name="send_email",
        description=(
            "Send an email to one or more recipients. "
            "Recipients must be in your approved contacts (Settings → Contacts). "
            "Supports plain text or HTML content, CC, BCC, and reply-to addresses."
        ),
    ),
]
