"""SMS-related tools for sending text messages via Twilio."""

import base64
import logging
from typing import Any
from typing import Dict
from typing import Optional

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


def send_sms(
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    message: str,
    status_callback: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an SMS message via Twilio API.

    This tool uses the Twilio Programmable Messaging API to send SMS messages.
    Phone numbers must be in E.164 format (+[country code][number], e.g., +14155552671).

    Args:
        account_sid: Twilio Account SID (starts with 'AC', 34 characters)
        auth_token: Twilio Auth Token (32 hexadecimal characters)
        from_number: Sender phone number in E.164 format (must be a Twilio number)
        to_number: Recipient phone number in E.164 format
        message: SMS message body (max 1600 characters)
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
        ...     account_sid="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        ...     auth_token="your_auth_token_here",
        ...     from_number="+14155552671",
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
        # Validate inputs
        if not account_sid or not account_sid.startswith("AC") or len(account_sid) != 34:
            return {
                "success": False,
                "error_message": "Invalid Account SID format. Must start with 'AC' and be 34 characters long",
                "from_number": from_number,
                "to_number": to_number,
            }

        if not auth_token or len(auth_token) != 32:
            return {
                "success": False,
                "error_message": "Invalid Auth Token format. Must be 32 characters long",
                "from_number": from_number,
                "to_number": to_number,
            }

        # Validate phone numbers (E.164 format)
        if not from_number or not from_number.startswith("+") or not from_number[1:].isdigit():
            return {
                "success": False,
                "error_message": f"Invalid from_number format. Must be E.164 format (e.g., +14155552671). Got: {from_number}",
                "from_number": from_number,
                "to_number": to_number,
            }

        if not to_number or not to_number.startswith("+") or not to_number[1:].isdigit():
            return {
                "success": False,
                "error_message": f"Invalid to_number format. Must be E.164 format (e.g., +14155552671). Got: {to_number}",
                "from_number": from_number,
                "to_number": to_number,
            }

        # Validate message
        if not message:
            return {
                "success": False,
                "error_message": "Message body cannot be empty",
                "from_number": from_number,
                "to_number": to_number,
            }

        if len(message) > 1600:
            return {
                "success": False,
                "error_message": f"Message too long ({len(message)} characters). Maximum is 1600 characters",
                "from_number": from_number,
                "to_number": to_number,
            }

        # Validate status callback if provided
        if status_callback and not (status_callback.startswith("http://") or status_callback.startswith("https://")):
            return {
                "success": False,
                "error_message": "Status callback URL must start with http:// or https://",
                "from_number": from_number,
                "to_number": to_number,
            }

        # Build Twilio API URL
        api_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

        # Build request payload (Twilio uses form data, not JSON)
        payload = {
            "From": from_number,
            "To": to_number,
            "Body": message,
        }

        if status_callback:
            payload["StatusCallback"] = status_callback

        # Create HTTP Basic Auth header
        credentials = f"{account_sid}:{auth_token}"
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
            return {
                "success": True,
                "message_sid": response_data.get("sid"),
                "status": response_data.get("status"),
                "from_number": response_data.get("from"),
                "to_number": response_data.get("to"),
                "date_created": response_data.get("date_created"),
                "segments": response_data.get("num_segments", "unknown"),
                "price": response_data.get("price"),
                "price_unit": response_data.get("price_unit"),
            }
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

                result = {
                    "success": False,
                    "error_code": error_code,
                    "error_message": error_message,
                    "status_code": response.status_code,
                    "from_number": from_number,
                    "to_number": to_number,
                }

                if more_info:
                    result["more_info"] = more_info

                return result

            except Exception:
                # Could not parse error response
                logger.error(f"Twilio API returned status {response.status_code}: {response.text[:500]}")
                return {
                    "success": False,
                    "error_message": f"Twilio API error (status {response.status_code})",
                    "status_code": response.status_code,
                    "from_number": from_number,
                    "to_number": to_number,
                }

    except httpx.TimeoutException:
        logger.error(f"Timeout sending SMS to {to_number}")
        return {
            "success": False,
            "error_message": "Request timed out after 30 seconds",
            "from_number": from_number,
            "to_number": to_number,
        }
    except httpx.RequestError as e:
        logger.error(f"Request error sending SMS to {to_number}: {e}")
        return {
            "success": False,
            "error_message": f"Request failed: {str(e)}",
            "from_number": from_number,
            "to_number": to_number,
        }
    except Exception as e:
        logger.exception(f"Unexpected error sending SMS to {to_number}")
        return {
            "success": False,
            "error_message": f"Unexpected error: {str(e)}",
            "from_number": from_number,
            "to_number": to_number,
        }


TOOLS = [
    StructuredTool.from_function(
        func=send_sms,
        name="send_sms",
        description=(
            "Send an SMS message via Twilio. Requires Twilio account credentials "
            "and phone numbers in E.164 format (+[country code][number]). "
            "Returns message SID and status if successful."
        ),
    ),
]
