#!/usr/bin/env python3
"""Session start hook for Claude Code workers.

Logs session starts and injects context for worker execution.
Can be extended to log to Life Hub or other tracking systems.

Environment variables:
  ZERG_RUN_ID: The current run ID (set by agent_runner)
  ZERG_TRACE_ID: End-to-end trace ID for debugging
  ZERG_WORKSPACE_PATH: Path to the workspace directory
"""

import json
import os
import sys
from datetime import datetime


def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        hook_input = {}

    session_id = hook_input.get("session_id", "unknown")
    cwd = hook_input.get("cwd", os.getcwd())

    # Get Zerg context from environment
    run_id = os.environ.get("ZERG_RUN_ID", "")
    trace_id = os.environ.get("ZERG_TRACE_ID", "")
    workspace_path = os.environ.get("ZERG_WORKSPACE_PATH", "")

    # Log session start (can be extended to send to Life Hub)
    timestamp = datetime.utcnow().isoformat()
    log_entry = {
        "event": "session_start",
        "timestamp": timestamp,
        "session_id": session_id,
        "cwd": cwd,
        "run_id": run_id,
        "trace_id": trace_id,
        "workspace_path": workspace_path,
    }

    # Print to stderr for logging (stdout is for Claude)
    print(f"[SESSION] {json.dumps(log_entry)}", file=sys.stderr)

    # Optionally output context for Claude via stdout
    # This gets injected into the session
    context = {}

    if run_id:
        context["run_id"] = run_id

    if workspace_path:
        context["workspace"] = workspace_path

    if context:
        # Return context that Claude can see (hookSpecificOutput format)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"Zerg worker context: {json.dumps(context)}"
            }
        }
        print(json.dumps(output))

    sys.exit(0)


if __name__ == "__main__":
    main()
