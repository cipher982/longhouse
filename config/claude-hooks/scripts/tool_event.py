#!/usr/bin/env python3
"""Claude Code hook for streaming tool events to Longhouse.

This script runs as a Claude Code hook (PreToolUse, PostToolUse, PostToolUseFailure)
and POSTs tool events to Longhouse for real-time visibility.

Environment variables:
  LONGHOUSE_CALLBACK_URL: Base URL for Longhouse API (e.g., http://localhost:47300)
  COMMIS_JOB_ID: The commis job ID for correlation
  COMMIS_CALLBACK_TOKEN: Optional auth token for the callback

The hook reads JSON from stdin with the tool event data and POSTs it to:
  POST {LONGHOUSE_CALLBACK_URL}/api/internal/commis/tool_event
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone


def main():
    # Get environment config
    callback_url = os.environ.get("LONGHOUSE_CALLBACK_URL")
    job_id = os.environ.get("COMMIS_JOB_ID")
    callback_token = os.environ.get("COMMIS_CALLBACK_TOKEN", "")

    # If no callback URL configured, silently exit (hook is disabled)
    if not callback_url or not job_id:
        sys.exit(0)

    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Invalid input, exit silently to not block Claude
        sys.exit(0)

    # Extract event details
    event_name = hook_input.get("hook_event_name", "unknown")
    session_id = hook_input.get("session_id", "")
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    tool_use_id = hook_input.get("tool_use_id", "")

    # For PostToolUse, also capture the response
    tool_response = hook_input.get("tool_response")

    # For PostToolUseFailure, capture the error
    error = hook_input.get("error")

    # Build event payload
    payload = {
        "job_id": job_id,
        "event_type": event_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    }

    # Add optional fields
    if tool_response is not None:
        # Truncate large responses to avoid payload bloat
        if isinstance(tool_response, str) and len(tool_response) > 10000:
            payload["tool_response"] = tool_response[:10000] + "\n... [truncated]"
        else:
            payload["tool_response"] = tool_response

    if error:
        payload["error"] = error

    # POST to Longhouse
    endpoint = f"{callback_url.rstrip('/')}/api/internal/commis/tool_event"

    headers = {
        "Content-Type": "application/json",
    }
    if callback_token:
        headers["Authorization"] = f"Bearer {callback_token}"

    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            # Success - response code 2xx
            pass
    except Exception as e:
        # Log to stderr for debugging but don't block Claude
        print(f"[HOOK] Failed to POST tool event: {e}", file=sys.stderr)

    # Always exit 0 to not block Claude execution
    sys.exit(0)


if __name__ == "__main__":
    main()
