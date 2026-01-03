"""Email-related tools for sending emails via AWS SES."""

import logging
import re
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
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import connector_not_configured_error
from zerg.tools.error_envelope import invalid_credentials_error
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)

# Default AWS region for SES
DEFAULT_AWS_REGION = "us-east-1"


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
    access_key_id: Optional[str] = None,
    secret_access_key: Optional[str] = None,
    region: Optional[str] = None,
    from_email: Optional[str] = None,
    reply_to: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Send an email using AWS Simple Email Service (SES).

    This tool allows agents to send emails via AWS SES.
    Credentials can be configured in Agent Settings -> Connectors or provided directly.

    Important notes:
    - Email addresses must be verified in SES (or domain must be verified)
    - New SES accounts are in sandbox mode (can only send to verified addresses)
    - Production access requires leaving sandbox mode
    - Rate limits depend on your SES account configuration

    Args:
        to: Recipient email address(es) - string or list
        subject: Email subject line
        text: Plain text email content (optional if html provided)
        html: HTML email content (optional if text provided)
        access_key_id: AWS Access Key ID - optional if configured in Agent Settings
        secret_access_key: AWS Secret Access Key - optional if configured in Agent Settings
        region: AWS region (defaults to us-east-1) - optional if configured in Agent Settings
        from_email: Sender email address (must be verified in SES) - optional if configured in Agent Settings
        reply_to: Reply-to email address (optional)
        cc: CC recipient email address(es) - string or list (optional)
        bcc: BCC recipient email address(es) - string or list (optional)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if email was sent
        - message_id: Unique message ID from SES (if successful)
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
        resolved_access_key = access_key_id
        resolved_secret_key = secret_access_key
        resolved_region = region
        resolved_from_email = from_email

        if not resolved_access_key or not resolved_secret_key or not resolved_from_email:
            resolver = get_credential_resolver()
            if resolver:
                creds = resolver.get(ConnectorType.EMAIL)
                if creds:
                    resolved_access_key = resolved_access_key or creds.get("access_key_id")
                    resolved_secret_key = resolved_secret_key or creds.get("secret_access_key")
                    resolved_region = resolved_region or creds.get("region")
                    resolved_from_email = resolved_from_email or creds.get("from_email")

        # Use default region if not specified
        resolved_region = resolved_region or DEFAULT_AWS_REGION

        # Validate required credentials
        if not resolved_access_key or not resolved_secret_key:
            return connector_not_configured_error("email", "Email (AWS SES)")
        if not resolved_from_email:
            return tool_error(
                error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
                user_message="From email not configured. Set it up in Settings → Integrations → Email.",
                connector="email",
                setup_url="/settings/integrations",
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
        return tool_success({"message_id": message_id})

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"SES API error ({error_code}): {error_message}")

        # Map error codes to error types
        if error_code in ("InvalidClientTokenId", "SignatureDoesNotMatch"):
            return invalid_credentials_error("email", "Email (AWS SES)")
        elif error_code == "AccessDenied":
            return tool_error(
                error_type=ErrorType.PERMISSION_DENIED,
                user_message="Access denied. Check IAM permissions for SES.",
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
                user_message="Rate limit exceeded. SES sending quota reached.",
                connector="email",
            )
        else:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message=f"SES error ({error_code}): {error_message}",
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
            "Send an email using AWS SES. "
            "Supports text/HTML content, CC/BCC, and reply-to. "
            "Credentials can be configured in Agent Settings -> Connectors or provided as parameters. "
            "Requires verified email/domain in SES."
        ),
    ),
]
