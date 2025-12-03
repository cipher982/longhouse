"""SMS-related tools for sending text messages via Twilio."""

import base64
import logging
from typing import Any
from typing import Dict
from typing import Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import (
    tool_error,
    tool_success,
    connector_not_configured_error,
    invalid_credentials_error,
    ErrorType,
)

logger = logging.getLogger(__name__)


def send_sms(
    to_number: str,
    message: str,
    account_sid: Optional[str] = None,
    auth_token: Optional[str] = None,
    from_number: Optional[str] = None,
    status_callback: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an SMS message via Twilio API.

    This tool uses the Twilio Programmable Messaging API to send SMS messages.
    Phone numbers must be in E.164 format (+[country code][number], e.g., +14155552671).

    Credentials can be provided as parameters or configured in Agent Settings -> Connectors.
    If configured, the tool will automatically use those credentials.

    Args:
        to_number: Recipient phone number in E.164 format
        message: SMS message body (max 1600 characters)
        account_sid: Twilio Account SID (optional if configured in Agent Settings)
        auth_token: Twilio Auth Token (optional if configured in Agent Settings)
        from_number: Sender phone number in E.164 format (optional if configured in Agent Settings)
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
            "User-Agent": "Zerg-Agent/1.0",
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
            return tool_success({
                "message_sid": response_data.get("sid"),
                "status": response_data.get("status"),
                "from_number": response_data.get("from"),
                "to_number": response_data.get("to"),
                "date_created": response_data.get("date_created"),
                "segments": response_data.get("num_segments", "unknown"),
                "price": response_data.get("price"),
                "price_unit": response_data.get("price_unit"),
            })
        else:
            # Error response from Twilio
            try:
                error_data = response.json()
                error_code = error_data.get("code")
                error_message = error_data.get("message", "Unknown error")
                more_info = error_data.get("more_info")

                logger.error(
                    f"Twilio API error {error_code}: {error_message}. "
                    f"Status: {response.status_code}. More info: {more_info}"
                )

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
            "Send an SMS message via Twilio. Credentials can be provided as parameters "
            "or configured in Agent Settings -> Connectors (SMS). "
            "Phone numbers must be in E.164 format (+[country code][number]). "
            "Returns message SID and status if successful."
        ),
    ),
]
