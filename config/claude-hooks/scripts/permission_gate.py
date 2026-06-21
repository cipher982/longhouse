#!/usr/bin/env python3
"""Claude Code PreToolUse hook: route tool-permission decisions through Longhouse.

When a managed Claude session is about to use a tool, this hook registers a held
permission request with Longhouse and blocks while long-polling for a decision.
When Longhouse resolves the request (allow/deny), the hook returns the matching
``permissionDecision`` to Claude.

Environment (set by the managed Claude launcher):
  LONGHOUSE_HOOK_URL          Base URL for the Longhouse API.
  LONGHOUSE_HOOK_TOKEN        Managed-local hook token (X-Agents-Token).
  LONGHOUSE_MANAGED_SESSION_ID  Longhouse session UUID (falls back to the hook's
                              own session_id field when absent).
  LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S  Max seconds to wait for a decision
                              (default 25; keep under Claude's hook timeout).
  LONGHOUSE_PERMISSION_HOOK_ENABLED    Set to "0"/"false" to disable the gate.

SAFETY — this hook can gate a real tool execution, so it MUST fail open:
  * If unconfigured, disabled, or anything goes wrong (network error, bad input,
    timeout with no decision), it emits NO decision and exits 0. Claude then
    falls back to its normal permission flow (the human is prompted) — it never
    auto-allows and never hangs the terminal indefinitely.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_DEFAULT_TIMEOUT_S = 25.0
_POLL_INTERVAL_S = 0.5
_REQUEST_TIMEOUT_S = 5.0


def _exit_no_decision() -> None:
    """Fail open: emit nothing, let Claude run its normal permission flow."""
    sys.exit(0)


def _emit_decision(decision: str, reason: str | None) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason or f"Longhouse {decision}",
        }
    }
    sys.stdout.write(json.dumps(payload))
    sys.exit(0)


def _enabled() -> bool:
    raw = str(os.environ.get("LONGHOUSE_PERMISSION_HOOK_ENABLED", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _post_json(url: str, body: dict, token: str) -> bool:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Agents-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
        return 200 <= resp.status < 300


def _get_decision(url: str, token: str) -> dict | None:
    headers = {}
    if token:
        headers["X-Agents-Token"] = token
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
        if not (200 <= resp.status < 300):
            return None
        return json.loads(resp.read().decode("utf-8") or "{}")


def main() -> None:
    if not _enabled():
        _exit_no_decision()

    base_url = str(os.environ.get("LONGHOUSE_HOOK_URL") or "").strip().rstrip("/")
    token = str(os.environ.get("LONGHOUSE_HOOK_TOKEN") or "").strip()
    if not base_url:
        _exit_no_decision()

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _exit_no_decision()
        return

    session_id = (
        str(os.environ.get("LONGHOUSE_MANAGED_SESSION_ID") or "").strip()
        or str(hook_input.get("session_id") or "").strip()
    )
    tool_use_id = str(hook_input.get("tool_use_id") or "").strip()
    tool_name = str(hook_input.get("tool_name") or "").strip()
    tool_input = hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    if not session_id or not tool_use_id:
        _exit_no_decision()

    try:
        timeout_s = float(os.environ.get("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_TIMEOUT_S

    # 1. Register the held permission request.
    try:
        registered = _post_json(
            f"{base_url}/api/agents/permission-requests",
            {
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            token,
        )
    except (urllib.error.URLError, OSError, ValueError):
        _exit_no_decision()
        return
    if not registered:
        _exit_no_decision()

    # 2. Long-poll for the decision, fail open on timeout.
    decision_url = f"{base_url}/api/agents/permission-decision?" + urllib.parse.urlencode(
        {"session_id": session_id, "tool_use_id": tool_use_id}
    )
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        try:
            result = _get_decision(decision_url, token)
        except (urllib.error.URLError, OSError, ValueError):
            _exit_no_decision()
            return
        if result and result.get("resolved"):
            decision = str(result.get("decision") or "").strip().lower()
            if decision in {"allow", "deny", "ask"}:
                _emit_decision(decision, result.get("reason"))
            _exit_no_decision()
        time.sleep(_POLL_INTERVAL_S)

    # 3. No decision in time → fail open to Claude's native prompt.
    _exit_no_decision()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute backstop: never block Claude on an unexpected hook error.
        sys.exit(0)
