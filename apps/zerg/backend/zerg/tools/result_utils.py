"""Utilities for processing tool execution results.

This module centralizes logic for:
- Detecting tool errors (legacy format, error_envelope)
- Redacting sensitive information from args/results
- Creating safe previews for logging/events
"""

import ast
import json
from typing import Any


def check_tool_error(result_content: Any) -> tuple[bool, str | None]:
    """Check if tool result indicates an error.

    Handles multiple error formats:
    1. Legacy: "<tool-error> ..." or "Error: ..."
    2. error_envelope: {"ok": false, "error_type": "...", "user_message": "..."}
       (works with both JSON and Python literal syntax)

    Parameters
    ----------
    result_content
        Tool result (any type - will be converted to string)

    Returns
    -------
    tuple[bool, str | None]
        (is_error, error_message) - error_message is None if not an error
    """
    # Convert to string (handles None, dicts, etc)
    if result_content is None:
        return False, None
    result_content = str(result_content)
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
            error_msg = parsed.get("user_message") or parsed.get("error_type") or "Tool returned ok=false"
            return True, error_msg

    return False, None


# Keys that should be redacted from event payloads to prevent secret leakage
SENSITIVE_KEYS = frozenset(
    {
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
    }
)


def redact_sensitive_args(args: Any) -> Any:
    """Redact sensitive fields from tool arguments for safe logging.

    Recursively walks dicts, lists, tuples, and sets to find and redact
    any values whose keys contain sensitive terms (api_key, token, secret, etc).

    Also detects key-value pair patterns like:
        {"key": "api_key", "value": "secret123"}
        {"title": "token", "value": "sk-live-..."}
        {"name": "Authorization", "value": "Bearer ..."}

    Where if the semantic key (key/title/name) contains a sensitive term,
    the corresponding value field is redacted.

    Parameters
    ----------
    args
        Tool arguments (dict, list, tuple, set, or primitive)

    Returns
    -------
    Same type as input
        Copy with sensitive values replaced with "[REDACTED]"
    """
    # Handle dict - check keys for sensitive terms
    if isinstance(args, dict):
        # Check for key-value pair pattern (common in Slack/Discord/headers)
        # Pattern: {"key": "Authorization", "value": "Bearer ..."}
        # Only applies when BOTH a semantic key AND value field are present
        has_value_field = "value" in args or "val" in args
        semantic_key = args.get("key") or args.get("title") or args.get("name")

        # Structural keys that should be exempt from redaction ONLY in pair patterns
        structural_keys_in_pattern = {"key", "title", "name", "type", "kind"}

        if semantic_key and isinstance(semantic_key, str) and has_value_field:
            semantic_lower = semantic_key.lower()
            # If the semantic key is a sensitive term, redact the value field
            if any(sensitive in semantic_lower for sensitive in SENSITIVE_KEYS):
                # Redact the value field while keeping structure
                redacted = {}
                for k, v in args.items():
                    if k in ("value", "val"):
                        redacted[k] = "[REDACTED]"
                    else:
                        # Don't redact the structural keys in this pattern
                        redacted[k] = redact_sensitive_args(v)
                return redacted

            # Key-value pair with NON-sensitive semantic key
            # Don't redact the structural key/title/name fields in this pattern
            redacted = {}
            for k, v in args.items():
                if k in structural_keys_in_pattern:
                    # Keep structural field as-is in pair pattern
                    redacted[k] = redact_sensitive_args(v)
                elif any(sensitive in k.lower() for sensitive in SENSITIVE_KEYS):
                    redacted[k] = "[REDACTED]"
                else:
                    redacted[k] = redact_sensitive_args(v)
            return redacted

        # Standard dict processing - check ALL keys for sensitive terms
        # No exemptions - even "key" field alone gets redacted if it contains a sensitive term
        redacted = {}
        for key, value in args.items():
            key_lower = key.lower()
            # Check if key contains any sensitive term
            if any(sensitive in key_lower for sensitive in SENSITIVE_KEYS):
                redacted[key] = "[REDACTED]"
            else:
                # Recursively redact the value
                redacted[key] = redact_sensitive_args(value)
        return redacted

    # Handle list - recurse into each element
    elif isinstance(args, list):
        return [redact_sensitive_args(item) for item in args]

    # Handle tuple - recurse and return tuple
    elif isinstance(args, tuple):
        return tuple(redact_sensitive_args(item) for item in args)

    # Handle set - recurse (though sets usually contain primitives)
    elif isinstance(args, set):
        # Sets can only contain hashable items, so dicts won't be in them
        # But we still try to redact in case of nested tuples
        return {redact_sensitive_args(item) for item in args}

    # Primitive value - return as-is
    else:
        return args


def is_critical_tool_error(
    result_content: str,
    error_msg: str | None,
    *,
    tool_name: str | None = None,
) -> bool:
    """Return True if a tool error is a "fail-fast" configuration/setup problem.

    This intentionally errs on the side of *not* failing fast so the fiche can:
    - correct bad arguments (validation errors)
    - try alternate tools (e.g. runner_exec -> ssh_exec)
    """
    # runner_exec has a natural fallback (ssh_exec) for single-user/dev setups.
    # Treat runner failures as non-critical so the model can attempt alternatives.
    if tool_name == "runner_exec":
        return False

    content_lower = (result_content or "").lower()
    msg_lower = (error_msg or "").lower()
    combined = f"{content_lower} {msg_lower}"

    # Connector/configuration errors (typically non-recoverable in-task)
    config_indicators = [
        "connector_not_configured",
        "not configured",
        "not connected",
        "invalid_credentials",
        "credentials have expired",
        "ssh client not found",
        "ssh key not found",
        "no ssh key",
        "not found in path",
    ]
    if any(indicator in combined for indicator in config_indicators):
        return True

    # Permission errors usually indicate missing key/rights.
    # (This can be "wrong user", but failing fast tends to be preferable here.)
    if "permission_denied" in combined or "permission denied" in combined:
        return True

    # Execution errors that strongly indicate infra/setup issues (SSH/host/network)
    if "execution_error" in combined and any(term in combined for term in ["ssh", "connection", "host", "unreachable"]):
        return True

    # Validation errors are usually recoverable (bad args/format); don't fail fast.
    if "validation_error" in combined:
        return False

    # Transient failures (timeouts, rate limits) are non-critical.
    transient_indicators = [
        "timeout",
        "timed out",
        "rate_limited",
        "rate limit",
        "temporarily unavailable",
    ]
    if any(indicator in combined for indicator in transient_indicators):
        return False

    return False


def safe_preview(content: Any, max_len: int = 200) -> str:
    """Create a safe preview of content, truncating if needed.

    Parameters
    ----------
    content
        Content to preview (any type - will be converted to string)
    max_len
        Maximum length (default 200 chars)

    Returns
    -------
    str
        Truncated content with "..." if needed
    """
    # Convert to string (handles None, dicts, etc)
    if content is None:
        return "(None)"
    content_str = str(content)

    if len(content_str) <= max_len:
        return content_str
    return content_str[: max_len - 3] + "..."


__all__ = [
    "check_tool_error",
    "is_critical_tool_error",
    "redact_sensitive_args",
    "safe_preview",
    "SENSITIVE_KEYS",
]
