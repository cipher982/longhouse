"""Utilities for processing tool execution results.

This module centralizes logic for:
- Detecting tool errors (legacy format, error_envelope)
- Redacting sensitive information from args/results
- Creating safe previews for logging/events
"""

import ast
import json


def check_tool_error(result_content: str) -> tuple[bool, str | None]:
    """Check if tool result indicates an error.

    Handles multiple error formats:
    1. Legacy: "<tool-error> ..." or "Error: ..."
    2. error_envelope: {"ok": false, "error_type": "...", "user_message": "..."}
       (works with both JSON and Python literal syntax)

    Parameters
    ----------
    result_content
        String representation of the tool result

    Returns
    -------
    tuple[bool, str | None]
        (is_error, error_message) - error_message is None if not an error
    """
    # Legacy format check
    if result_content.startswith("<tool-error>"):
        return True, result_content
    if result_content.startswith("Error:"):
        return True, result_content

    # error_envelope format check - try both JSON and Python literal
    if result_content.startswith("{"):
        parsed = None

        # Try JSON first (double quotes)
        try:
            parsed = json.loads(result_content)
        except (json.JSONDecodeError, TypeError):
            # Try Python literal (single quotes) - this is what str(dict) produces
            try:
                parsed = ast.literal_eval(result_content)
            except (ValueError, SyntaxError):
                pass  # Neither JSON nor valid Python literal

        # Check if parsed dict indicates error
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            # Extract user_message if available, else use error_type
            error_msg = (
                parsed.get("user_message")
                or parsed.get("error_type")
                or "Tool returned ok=false"
            )
            return True, error_msg

    return False, None


# Keys that should be redacted from event payloads to prevent secret leakage
SENSITIVE_KEYS = frozenset({
    "key",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "auth",
    "authorization",
    "bearer",
    "private_key",
    "privatekey",
    "access_token",
    "refresh_token",
})


def redact_sensitive_args(args: dict) -> dict:
    """Redact sensitive fields from tool arguments for safe logging.

    Parameters
    ----------
    args
        Tool arguments dict (may be nested)

    Returns
    -------
    dict
        Copy of args with sensitive values replaced with "[REDACTED]"
    """
    if not isinstance(args, dict):
        return {"_raw": "[non-dict args]"}

    redacted = {}
    for key, value in args.items():
        key_lower = key.lower()
        # Check if key contains any sensitive term
        if any(sensitive in key_lower for sensitive in SENSITIVE_KEYS):
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_sensitive_args(value)
        else:
            redacted[key] = value
    return redacted


def safe_preview(content: str, max_len: int = 200) -> str:
    """Create a safe preview of content, truncating if needed.

    Parameters
    ----------
    content
        Content to preview
    max_len
        Maximum length (default 200 chars)

    Returns
    -------
    str
        Truncated content with "..." if needed
    """
    if len(content) <= max_len:
        return content
    return content[: max_len - 3] + "..."


__all__ = [
    "check_tool_error",
    "redact_sensitive_args",
    "safe_preview",
    "SENSITIVE_KEYS",
]
