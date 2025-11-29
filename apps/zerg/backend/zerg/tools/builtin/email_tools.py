"""Email-related tools for sending emails via Resend API."""

import logging
import re
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

# Resend API constants
RESEND_API_URL = "https://api.resend.com/emails"


def _validate_email(email: str) -> bool:
    """Validate email address format.

    Args:
        email: Email address to validate

    Returns:
        True if valid, False otherwise
    """
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


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
        emails = [emails]

    validated = []
    for email in emails:
        email = email.strip()
        if not _validate_email(email):
            raise ValueError(f"Invalid email address: {email}")
        validated.append(email)

    return validated


def send_email(
    api_key: str,
    from_email: str,
    to: Union[str, List[str]],
    subject: str,
    text: Optional[str] = None,
    html: Optional[str] = None,
    reply_to: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Send an email using the Resend API.

    This tool allows agents to send emails via the Resend email service.
    Requires a valid Resend API key (get one at resend.com).

    Important notes:
    - Domain verification required before sending (see resend.com/domains)
    - Free tier: 100 emails/day, 3,000/month
    - Rate limit: 2 requests/second (can be increased)
    - Must maintain <4% bounce rate and <0.08% spam rate

    Args:
        api_key: Resend API key (starts with 're_')
        from_email: Sender email address (must be from verified domain)
        to: Recipient email address(es) - string or list
        subject: Email subject line
        text: Plain text email content (optional if html provided)
        html: HTML email content (optional if text provided)
        reply_to: Reply-to email address (optional)
        cc: CC recipient email address(es) - string or list (optional)
        bcc: BCC recipient email address(es) - string or list (optional)
        attachments: List of attachment dicts with 'filename' and 'content' or 'path' (optional)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if email was sent
        - message_id: Unique message ID from Resend (if successful)
        - error: Error message (if failed)

    Example:
        >>> send_email(
        ...     api_key="re_xxxxxxxxx",
        ...     from_email="noreply@mydomain.com",
        ...     to="user@example.com",
        ...     subject="Welcome!",
        ...     html="<h1>Welcome to our service!</h1>",
        ...     reply_to="support@mydomain.com"
        ... )
        {"success": True, "message_id": "abc123..."}
    """
    try:
        # Validate API key format
        if not api_key or not api_key.startswith("re_"):
            logger.error("Invalid API key format")
            return {
                "success": False,
                "error": "Invalid API key format. Resend API keys start with 're_'",
            }

        # Validate required fields
        if not from_email:
            return {"success": False, "error": "from_email is required"}

        if not to:
            return {"success": False, "error": "to is required"}

        if not subject:
            return {"success": False, "error": "subject is required"}

        if not text and not html:
            return {
                "success": False,
                "error": "Either text or html content is required",
            }

        # Validate from_email
        if not _validate_email(from_email):
            logger.error(f"Invalid from_email: {from_email}")
            return {"success": False, "error": f"Invalid from_email: {from_email}"}

        # Validate to addresses
        try:
            to_list = _validate_email_list(to)
        except ValueError as e:
            logger.error(f"Invalid to addresses: {e}")
            return {"success": False, "error": str(e)}

        # Build request payload
        payload = {
            "from": from_email,
            "to": to_list,
            "subject": subject,
        }

        # Add optional fields
        if text:
            payload["text"] = text

        if html:
            payload["html"] = html

        if reply_to:
            if not _validate_email(reply_to):
                logger.error(f"Invalid reply_to: {reply_to}")
                return {"success": False, "error": f"Invalid reply_to: {reply_to}"}
            payload["reply_to"] = reply_to

        if cc:
            try:
                payload["cc"] = _validate_email_list(cc)
            except ValueError as e:
                logger.error(f"Invalid CC addresses: {e}")
                return {"success": False, "error": f"CC validation: {str(e)}"}

        if bcc:
            try:
                payload["bcc"] = _validate_email_list(bcc)
            except ValueError as e:
                logger.error(f"Invalid BCC addresses: {e}")
                return {"success": False, "error": f"BCC validation: {str(e)}"}

        if attachments:
            if not isinstance(attachments, list):
                return {
                    "success": False,
                    "error": "attachments must be a list of dicts",
                }
            payload["attachments"] = attachments

        # Prepare headers
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make API request
        logger.info(f"Sending email from {from_email} to {to_list}")
        with httpx.Client() as client:
            response = client.post(
                RESEND_API_URL,
                json=payload,
                headers=headers,
                timeout=30.0,
            )

        # Handle response
        if response.status_code == 200:
            response_data = response.json()
            message_id = response_data.get("id", "unknown")
            logger.info(f"Email sent successfully: {message_id}")
            return {
                "success": True,
                "message_id": message_id,
            }
        else:
            # Parse error response
            try:
                error_data = response.json()
                error_message = error_data.get("message", response.text)
            except Exception:
                error_message = response.text

            logger.error(f"Resend API error ({response.status_code}): {error_message}")

            # Provide helpful error messages for common issues
            if response.status_code == 401:
                error_message = "Invalid API key. Check your Resend API key."
            elif response.status_code == 403:
                error_message = "Access forbidden. Verify your domain is verified in Resend."
            elif response.status_code == 422:
                error_message = f"Validation error: {error_message}"
            elif response.status_code == 429:
                error_message = "Rate limit exceeded. Slow down requests or upgrade plan."

            return {
                "success": False,
                "error": f"Resend API error ({response.status_code}): {error_message}",
            }

    except httpx.TimeoutException:
        logger.error(f"Timeout sending email to {to}")
        return {
            "success": False,
            "error": "Request timed out after 30 seconds",
        }
    except httpx.RequestError as e:
        logger.error(f"Request error sending email: {e}")
        return {
            "success": False,
            "error": f"Request failed: {str(e)}",
        }
    except Exception as e:
        logger.exception("Unexpected error sending email")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
        }


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=send_email,
        name="send_email",
        description=(
            "Send an email using the Resend API. "
            "Supports text/HTML content, CC/BCC, reply-to, and attachments. "
            "Requires verified domain in Resend account."
        ),
    ),
]
