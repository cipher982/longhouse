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

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import connector_not_configured_error
from zerg.tools.error_envelope import invalid_credentials_error
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

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
    to: Union[str, List[str]],
    subject: str,
    text: Optional[str] = None,
    html: Optional[str] = None,
    api_key: Optional[str] = None,
    from_email: Optional[str] = None,
    reply_to: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Send an email using the Resend API.

    This tool allows agents to send emails via the Resend email service.
    Credentials can be configured in Agent Settings -> Connectors or provided directly.

    Important notes:
    - Domain verification required before sending (see resend.com/domains)
    - Free tier: 100 emails/day, 3,000/month
    - Rate limit: 2 requests/second (can be increased)
    - Must maintain <4% bounce rate and <0.08% spam rate

    Args:
        to: Recipient email address(es) - string or list
        subject: Email subject line
        text: Plain text email content (optional if html provided)
        html: HTML email content (optional if text provided)
        api_key: Resend API key (starts with 're_') - optional if configured in Agent Settings
        from_email: Sender email address (must be from verified domain) - optional if configured in Agent Settings
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
        ...     to="user@example.com",
        ...     subject="Welcome!",
        ...     html="<h1>Welcome to our service!</h1>",
        ...     reply_to="support@mydomain.com"
        ... )
        {"success": True, "message_id": "abc123..."}
    """
    try:
        # Try to get credentials from context if not provided
        resolved_api_key = api_key
        resolved_from_email = from_email
        if not resolved_api_key or not resolved_from_email:
            resolver = get_credential_resolver()
            if resolver:
                creds = resolver.get(ConnectorType.EMAIL)
                if creds:
                    resolved_api_key = resolved_api_key or creds.get("api_key")
                    resolved_from_email = resolved_from_email or creds.get("from_email")

        # Validate required credentials
        if not resolved_api_key:
            return connector_not_configured_error("email", "Email (Resend)")
        if not resolved_from_email:
            return tool_error(
                error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
                user_message="From email not configured. Set it up in Settings → Integrations → Email.",
                connector="email",
                setup_url="/settings/integrations",
            )

        # Validate API key format
        if not resolved_api_key.startswith("re_"):
            logger.error("Invalid API key format")
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Invalid API key format. Resend API keys start with 're_'",
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

        # Build request payload
        payload = {
            "from": resolved_from_email,
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
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"Invalid reply_to: {reply_to}",
                    connector="email",
                )
            payload["reply_to"] = reply_to

        if cc:
            try:
                payload["cc"] = _validate_email_list(cc)
            except ValueError as e:
                logger.error(f"Invalid CC addresses: {e}")
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"CC validation: {str(e)}",
                    connector="email",
                )

        if bcc:
            try:
                payload["bcc"] = _validate_email_list(bcc)
            except ValueError as e:
                logger.error(f"Invalid BCC addresses: {e}")
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"BCC validation: {str(e)}",
                    connector="email",
                )

        if attachments:
            if not isinstance(attachments, list):
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message="attachments must be a list of dicts",
                    connector="email",
                )
            payload["attachments"] = attachments

        # Prepare headers
        headers = {
            "Authorization": f"Bearer {resolved_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make API request
        logger.info(f"Sending email from {resolved_from_email} to {to_list}")
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
            return tool_success({"message_id": message_id})
        else:
            # Parse error response
            try:
                error_data = response.json()
                error_message = error_data.get("message", response.text)
            except Exception:
                error_message = response.text

            logger.error(f"Resend API error ({response.status_code}): {error_message}")

            # Map status codes to error types
            if response.status_code == 401:
                return invalid_credentials_error("email", "Email (Resend)")
            elif response.status_code == 403:
                return tool_error(
                    error_type=ErrorType.PERMISSION_DENIED,
                    user_message="Access forbidden. Verify your domain is verified in Resend.",
                    connector="email",
                )
            elif response.status_code == 422:
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message=f"Validation error: {error_message}",
                    connector="email",
                )
            elif response.status_code == 429:
                return tool_error(
                    error_type=ErrorType.RATE_LIMITED,
                    user_message="Rate limit exceeded. Slow down requests or upgrade plan.",
                    connector="email",
                )
            else:
                return tool_error(
                    error_type=ErrorType.EXECUTION_ERROR,
                    user_message=f"Resend API error ({response.status_code}): {error_message}",
                    connector="email",
                )

    except httpx.TimeoutException:
        logger.error(f"Timeout sending email to {to}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="Request timed out after 30 seconds",
            connector="email",
        )
    except httpx.RequestError as e:
        logger.error(f"Request error sending email: {e}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Request failed: {str(e)}",
            connector="email",
        )
    except Exception as e:
        logger.exception("Unexpected error sending email")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Unexpected error: {str(e)}",
            connector="email",
        )


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=send_email,
        name="send_email",
        description=(
            "Send an email using the Resend API. "
            "Supports text/HTML content, CC/BCC, reply-to, and attachments. "
            "Credentials can be configured in Agent Settings -> Connectors or provided as parameters. "
            "Requires verified domain in Resend account."
        ),
    ),
]
