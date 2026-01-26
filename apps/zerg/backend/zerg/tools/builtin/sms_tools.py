"""SMS-related tools for sending text messages via Twilio.

Includes approved contacts validation and rate limiting.
"""

import base64
import logging
import os
import re
from typing import Any
from typing import Dict
from typing import Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.context import get_commis_context
from zerg.database import db_session
from zerg.models.models import User
from zerg.models.models import UserDailySmsCounter
from zerg.models.models import UserPhoneContact
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import connector_not_configured_error
from zerg.tools.error_envelope import invalid_credentials_error
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)

# Default daily SMS limit
DEFAULT_DAILY_SMS_LIMIT = 10


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


def _normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format.

    Strips non-digit characters except leading +.
    """
    phone = phone.strip()
    if phone.startswith("+"):
        # Keep + prefix, strip everything else except digits
        return "+" + re.sub(r"[^\d]", "", phone[1:])
    else:
        # No + prefix, just strip non-digits
        digits = re.sub(r"[^\d]", "", phone)
        # Assume US if 10 digits without +
        if len(digits) == 10:
            return "+1" + digits
        return "+" + digits


def _validate_phone_format(phone: str) -> bool:
    """Validate phone number is in E.164 format."""
    pattern = r"^\+\d{10,15}$"
    return bool(re.match(pattern, phone))


def _is_approved_recipient(user_id: int, phone: str, db) -> bool:
    """Check if normalized phone is user's own or in approved contacts.

    Args:
        user_id: User ID to check contacts for
        phone: Phone number to check
        db: Database session

    Returns:
        True if approved, False otherwise
    """
    normalized = _normalize_phone(phone)

    # Check user's own phone (if stored in user profile - for future support)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        # Note: User model may not have phone field yet - this is future-proofed
        user_phone = getattr(user, "phone", None)
        if user_phone and _normalize_phone(user_phone) == normalized:
            return True

    # Check approved contacts (stored normalized)
    contact = (
        db.query(UserPhoneContact)
        .filter(
            UserPhoneContact.owner_id == user_id,
            UserPhoneContact.phone_normalized == normalized,
        )
        .first()
    )
    return contact is not None


def _reserve_rate_limit(user_id: int, db) -> tuple[bool, str]:
    """Atomically check and reserve rate limit BEFORE sending.

    Uses SELECT FOR UPDATE to prevent race conditions.
    Uses INSERT ON CONFLICT to handle concurrent counter creation.
    Returns (allowed, error_message).
    """
    from datetime import datetime
    from datetime import timezone

    from sqlalchemy.dialects.postgresql import insert

    today = datetime.now(timezone.utc).date()

    # Upsert counter to handle race condition on creation
    stmt = (
        insert(UserDailySmsCounter)
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
        db.query(UserDailySmsCounter)
        .filter(
            UserDailySmsCounter.user_id == user_id,
            UserDailySmsCounter.date == today,
        )
        .with_for_update()
        .first()
    )

    limit = int(os.getenv("DAILY_SMS_LIMIT", str(DEFAULT_DAILY_SMS_LIMIT)))
    if counter.count + 1 > limit:
        return False, f"Daily SMS limit reached ({counter.count}/{limit}). Resets at midnight UTC."

    # Reserve the slot
    counter.count += 1
    db.flush()  # Holds lock until commit
    return True, ""


def send_sms(
    to_number: str,
    message: str,
    account_sid: Optional[str] = None,
    auth_token: Optional[str] = None,
    from_number: Optional[str] = None,
    status_callback: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an SMS message via Twilio API.

    Recipients must be in your approved contacts list (Settings → Contacts).
    Subject to daily rate limits.

    This tool uses the Twilio Programmable Messaging API to send SMS messages.
    Phone numbers must be in E.164 format (+[country code][number], e.g., +14155552671).

    Credentials can be provided as parameters or configured in Fiche Settings -> Connectors.
    If configured, the tool will automatically use those credentials.

    Args:
        to_number: Recipient phone number in E.164 format
        message: SMS message body (max 1600 characters)
        account_sid: Twilio Account SID (optional if configured in Fiche Settings)
        auth_token: Twilio Auth Token (optional if configured in Fiche Settings)
        from_number: Sender phone number in E.164 format (optional if configured in Fiche Settings)
        status_callback: Optional webhook URL to receive delivery status updates

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the SMS was queued successfully
        - message_sid: Twilio message SID (unique identifier) if successful
        - status: Message status from Twilio (e.g., 'queued', 'sent', 'delivered')
        - error_code: Twilio error code if request failed
        - error_message: Error message if request failed
        - from_number: The sender phone number used
        - to_number: The recipient phone number
        - segments: Estimated number of SMS segments (affects cost)

    Example:
        >>> send_sms(
        ...     to_number="+14155552672",
        ...     message="Hello from Zerg!",
        ... )
        {
            "success": True,
            "message_sid": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "status": "queued",
            "from_number": "+14155552671",
            "to_number": "+14155552672",
            "segments": 1
        }

    Notes:
        - Rate limit: 100 messages per second (default)
        - Costs apply per SMS segment sent
        - GSM-7 encoding: 160 chars/segment (153 for multi-part)
        - Unicode (UCS-2): 70 chars/segment (67 for multi-part)
        - Maximum message length: 1600 characters
        - Status callback requires a publicly accessible HTTPS endpoint
    """
    try:
        # Get user context for contact validation and rate limiting
        user_id = _get_user_id()

        # Try to get credentials from context if not provided
        resolved_account_sid = account_sid
        resolved_auth_token = auth_token
        resolved_from_number = from_number
        if not all([resolved_account_sid, resolved_auth_token, resolved_from_number]):
            resolver = get_credential_resolver()
            if resolver:
                creds = resolver.get(ConnectorType.SMS)
                if creds:
                    resolved_account_sid = resolved_account_sid or creds.get("account_sid")
                    resolved_auth_token = resolved_auth_token or creds.get("auth_token")
                    resolved_from_number = resolved_from_number or creds.get("from_number")

        # Validate required credentials
        if not resolved_account_sid:
            return connector_not_configured_error("sms", "SMS (Twilio)")
        if not resolved_auth_token:
            return tool_error(
                error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
                user_message="Twilio Auth Token not configured. Set it up in Settings → Integrations → SMS.",
                connector="sms",
                setup_url="/settings/integrations",
            )
        if not resolved_from_number:
            return tool_error(
                error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
                user_message="From phone number not configured. Set it up in Settings → Integrations → SMS.",
                connector="sms",
                setup_url="/settings/integrations",
            )

        # Validate inputs
        if not resolved_account_sid or not resolved_account_sid.startswith("AC") or len(resolved_account_sid) != 34:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Invalid Account SID format. Must start with 'AC' and be 34 characters long",
                connector="sms",
            )

        if not resolved_auth_token or len(resolved_auth_token) != 32:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Invalid Auth Token format. Must be 32 characters long",
                connector="sms",
            )

        # Validate phone numbers (E.164 format)
        if not resolved_from_number or not resolved_from_number.startswith("+") or not resolved_from_number[1:].isdigit():
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid from_number format. Must be E.164 format (e.g., +14155552671). Got: {resolved_from_number}",
                connector="sms",
            )

        if not to_number or not to_number.startswith("+") or not to_number[1:].isdigit():
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Invalid to_number format. Must be E.164 format (e.g., +14155552671). Got: {to_number}",
                connector="sms",
            )

        # Validate message
        if not message:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Message body cannot be empty",
                connector="sms",
            )

        if len(message) > 1600:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"Message too long ({len(message)} characters). Maximum is 1600 characters",
                connector="sms",
            )

        # Validate status callback if provided
        if status_callback and not (status_callback.startswith("http://") or status_callback.startswith("https://")):
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Status callback URL must start with http:// or https://",
                connector="sms",
            )

        # Contact validation and rate limiting (requires user context)
        # SECURITY: Fail closed - require user context for all sends
        if not user_id:
            logger.error("No user context for SMS send - rejecting for security")
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Unable to verify sender identity. Please try again.",
                connector="sms",
            )

        # Normalize to_number for contact lookup (handles display formats)
        normalized_to = _normalize_phone(to_number)

        with db_session() as db:
            # Validate recipient is approved (use normalized form)
            if not _is_approved_recipient(user_id, normalized_to, db):
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=(f"'{to_number}' is not in your approved contacts. " "Add them in Settings → Contacts before sending."),
                    connector="sms",
                )

            # Atomic rate limit check
            allowed, msg = _reserve_rate_limit(user_id, db)
            if not allowed:
                return tool_error(
                    error_type=ErrorType.RATE_LIMITED,
                    user_message=msg,
                    connector="sms",
                )

            # Commit to release lock
            db.commit()

        # Build Twilio API URL
        api_url = f"https://api.twilio.com/2010-04-01/Accounts/{resolved_account_sid}/Messages.json"

        # Build request payload (Twilio uses form data, not JSON)
        payload = {
            "From": resolved_from_number,
            "To": to_number,
            "Body": message,
        }

        if status_callback:
            payload["StatusCallback"] = status_callback

        # Create HTTP Basic Auth header
        credentials = f"{resolved_account_sid}:{resolved_auth_token}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        headers = {
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Fiche": "Zerg-Fiche/1.0",
        }

        # Make the API request
        with httpx.Client() as client:
            response = client.post(
                url=api_url,
                data=payload,  # Use data (not json) for form encoding
                headers=headers,
                timeout=30.0,
                follow_redirects=True,
            )

        # Parse response
        if response.status_code == 201:
            # Success - message was queued
            response_data = response.json()
            return tool_success(
                {
                    "message_sid": response_data.get("sid"),
                    "status": response_data.get("status"),
                    "from_number": response_data.get("from"),
                    "to_number": response_data.get("to"),
                    "date_created": response_data.get("date_created"),
                    "segments": response_data.get("num_segments", "unknown"),
                    "price": response_data.get("price"),
                    "price_unit": response_data.get("price_unit"),
                }
            )
        else:
            # Error response from Twilio
            try:
                error_data = response.json()
                error_code = error_data.get("code")
                error_message = error_data.get("message", "Unknown error")
                more_info = error_data.get("more_info")

                logger.error(f"Twilio API error {error_code}: {error_message}. " f"Status: {response.status_code}. More info: {more_info}")

                # Map status codes to error types
                if response.status_code == 401:
                    return invalid_credentials_error("sms", "SMS (Twilio)")
                elif response.status_code == 429:
                    return tool_error(
                        error_type=ErrorType.RATE_LIMITED,
                        user_message=error_message,
                        connector="sms",
                    )
                elif response.status_code == 403:
                    return tool_error(
                        error_type=ErrorType.PERMISSION_DENIED,
                        user_message=error_message,
                        connector="sms",
                    )
                elif response.status_code == 400:
                    return tool_error(
                        error_type=ErrorType.VALIDATION_ERROR,
                        user_message=error_message,
                        connector="sms",
                    )
                else:
                    return tool_error(
                        error_type=ErrorType.EXECUTION_ERROR,
                        user_message=error_message,
                        connector="sms",
                    )

            except Exception:
                # Could not parse error response
                logger.error(f"Twilio API returned status {response.status_code}: {response.text[:500]}")
                return tool_error(
                    error_type=ErrorType.EXECUTION_ERROR,
                    user_message=f"Twilio API error (status {response.status_code})",
                    connector="sms",
                )

    except httpx.TimeoutException:
        logger.error(f"Timeout sending SMS to {to_number}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Request timed out after 30 seconds",
            connector="sms",
        )
    except httpx.RequestError as e:
        logger.error(f"Request error sending SMS to {to_number}: {e}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Request failed: {str(e)}",
            connector="sms",
        )
    except Exception as e:
        logger.exception(f"Unexpected error sending SMS to {to_number}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Unexpected error: {str(e)}",
            connector="sms",
        )


TOOLS = [
    StructuredTool.from_function(
        func=send_sms,
        name="send_sms",
        description=(
            "Send an SMS message via Twilio. "
            "Recipients must be in your approved contacts (Settings → Contacts). "
            "Credentials can be provided as parameters or configured in Settings → Integrations (SMS). "
            "Phone numbers must be in E.164 format (+[country code][number]). "
            "Returns message SID and status if successful."
        ),
    ),
]
