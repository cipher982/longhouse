"""Contact user tool - allows fiches to notify their owner."""

import logging
from typing import Any
from typing import Dict

from langchain_core.tools import StructuredTool

from zerg.context import get_commis_context
from zerg.crud import crud
from zerg.database import db_session
from zerg.tools.builtin.email_tools import send_email
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)

# Priority to emoji mapping for email subject
PRIORITY_EMOJI = {
    "low": "",
    "normal": "",
    "high": "⚠️ ",
    "urgent": "🚨 ",
}


def _convert_markdown_to_html(text: str) -> str:
    """Convert basic markdown to HTML.

    Since we don't have a markdown library, we'll do simple text-to-HTML conversion.
    For more advanced markdown, we can add a dependency later.

    Args:
        text: Plain text or simple markdown

    Returns:
        HTML string
    """
    # Convert line breaks to HTML
    lines = text.split("\n")
    html_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            html_lines.append("<br>")
        elif line.startswith("# "):
            html_lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("## "):
            html_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("### "):
            html_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("- ") or line.startswith("* "):
            html_lines.append(f"<li>{line[2:]}</li>")
        else:
            # Simple inline formatting
            line = line.replace("**", "<strong>").replace("**", "</strong>")
            line = line.replace("*", "<em>").replace("*", "</em>")
            html_lines.append(f"<p>{line}</p>")

    return "\n".join(html_lines)


def _build_email_template(message: str, priority: str, commis_id: str | None = None) -> str:
    """Build HTML email template with Swarmlet branding.

    Args:
        message: The notification message
        priority: Priority level
        commis_id: Optional commis ID for context

    Returns:
        HTML email body
    """
    # Convert message to HTML
    message_html = _convert_markdown_to_html(message)

    # Build priority badge if not normal
    priority_badge = ""
    if priority != "normal":
        priority_color = {
            "low": "#6c757d",
            "high": "#fd7e14",
            "urgent": "#dc3545",
        }.get(priority, "#6c757d")

        priority_badge = f"""
        <div style="display: inline-block; background: {priority_color}; color: white;
                    padding: 4px 12px; border-radius: 4px; font-size: 12px;
                    font-weight: bold; margin-bottom: 16px;">
            {priority.upper()} PRIORITY
        </div>
        """

    # Build commis context if available
    commis_context = ""
    if commis_id:
        commis_context = f"""
        <div style="background: #f8f9fa; padding: 12px; border-radius: 4px;
                    margin-top: 24px; font-size: 13px; color: #6c757d;">
            <strong>Commis ID:</strong> {commis_id}
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                 line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">

        <!-- Header -->
        <div style="text-align: center; margin-bottom: 32px;">
            <h1 style="color: #2563eb; margin: 0; font-size: 24px;">Swarmlet</h1>
            <p style="color: #6c757d; margin: 4px 0 0 0; font-size: 14px;">Fiche Notification</p>
        </div>

        <!-- Priority Badge -->
        {priority_badge}

        <!-- Main Content -->
        <div style="background: white; padding: 24px; border-radius: 8px; border: 1px solid #e5e7eb;">
            {message_html}
        </div>

        {commis_context}

        <!-- Footer -->
        <div style="text-align: center; margin-top: 32px; padding-top: 24px;
                    border-top: 1px solid #e5e7eb; color: #6c757d; font-size: 13px;">
            <p>This notification was sent by your Swarmlet fiche.</p>
            <p style="margin-top: 8px;">
                <a href="https://swarmlet.com" style="color: #2563eb; text-decoration: none;">
                    Visit Dashboard
                </a>
            </p>
        </div>

    </body>
    </html>
    """


def contact_user(
    subject: str,
    message: str,
    priority: str = "normal",
) -> Dict[str, Any]:
    """Send a notification to the fiche's owner.

    Use this when:
    - A long-running task completes
    - You encounter an error that needs user attention
    - You need user input to proceed
    - Important events occur that the user should know about

    Args:
        subject: Email subject (will be prefixed with [Swarmlet])
        message: Message body (supports basic markdown: #headers, **bold**, *italic*, - lists)
        priority: Priority level - "low", "normal", "high", or "urgent" (default: "normal")

    Returns:
        Dictionary containing:
        - success: Boolean indicating if notification was sent
        - message_id: Unique message ID from email service (if successful)
        - error: Error message (if failed)

    Example:
        >>> contact_user(
        ...     subject="Backup completed",
        ...     message="Successfully backed up 1.2GB of data to S3.\\n\\n**Duration**: 5 minutes",
        ...     priority="normal"
        ... )
        {"success": True, "message_id": "abc123..."}
    """
    try:
        # Validate inputs
        if not subject or not subject.strip():
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="subject is required and cannot be empty",
            )

        if not message or not message.strip():
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="message is required and cannot be empty",
            )

        # Validate priority
        valid_priorities = ["low", "normal", "high", "urgent"]
        if priority not in valid_priorities:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message=f"priority must be one of: {', '.join(valid_priorities)}",
            )

        # Get commis context
        ctx = get_commis_context()
        if ctx is None:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="contact_user can only be called from within a commis context. "
                "This tool requires access to commis execution metadata.",
            )

        owner_id = ctx.owner_id
        if owner_id is None:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="No owner information available in commis context. Cannot determine who to notify.",
            )

        # Look up user
        with db_session() as db:
            user = crud.get_user(db, owner_id)
            if not user:
                logger.error(f"User {owner_id} not found")
                return tool_error(
                    error_type=ErrorType.EXECUTION_ERROR,
                    user_message=f"User account (ID: {owner_id}) not found. " "This may indicate a database inconsistency.",
                )

            # Check if user has email configured
            if not user.email:
                logger.warning(f"User {owner_id} has no email configured")
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message="Your account has no email address configured. "
                    "Please add an email address in your account settings to receive notifications.",
                )

            user_email = user.email
            _user_name = user.display_name or user.email.split("@")[0]  # Reserved for future personalization

        # Build email content
        priority_emoji = PRIORITY_EMOJI.get(priority, "")
        full_subject = f"{priority_emoji}[Swarmlet] {subject}"

        html_body = _build_email_template(
            message=message,
            priority=priority,
            commis_id=ctx.commis_id,
        )

        # Plain text fallback (strip HTML tags)
        text_body = message

        # Send email
        logger.info(f"Sending notification to user {owner_id} ({user_email}): {subject}")

        result = send_email(
            to=user_email,
            subject=full_subject,
            html=html_body,
            text=text_body,
        )

        # Check if email send was successful
        if not result.get("ok"):
            logger.error(f"Failed to send notification: {result.get('user_message')}")
            return result  # Return error from send_email

        # Extract message_id from the data field
        message_id = result.get("data", {}).get("message_id", "unknown")
        logger.info(f"Successfully sent notification to user {owner_id}: {message_id}")
        return tool_success({"message_id": message_id})

    except Exception as e:
        logger.exception("Unexpected error in contact_user")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Failed to send notification: {str(e)}",
        )


# Export as LangChain tool
TOOLS = [
    StructuredTool.from_function(
        func=contact_user,
        name="contact_user",
        description=(
            "Send a notification to the fiche's owner (user). "
            "Use this to notify about task completion, errors, or when you need user input. "
            "Supports priority levels: low, normal, high, urgent. "
            "Message supports basic markdown formatting."
        ),
    ),
]
