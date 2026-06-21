#!/usr/bin/env python3
"""Claude Code PreToolUse hook: route tool-permission decisions through Longhouse.

When a managed Claude session is about to use a tool, this hook registers a held
permission request with Longhouse and blocks while long-polling for a decision.
When Longhouse resolves the request (allow/deny), the hook returns the matching
``permissionDecision`` to Claude.

Environment (set by the managed Claude launcher):
  LONGHOUSE_HOOK_URL          Base URL for the Longhouse API.
  LONGHOUSE_HOOK_TOKEN        Session-scoped hook token (X-Agents-Token).
  LONGHOUSE_MANAGED_SESSION_ID  Longhouse session UUID (falls back to the hook's
                              own session_id field when absent).
  LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S  Max seconds to wait for a decision
                              (default 20, clamped <= 20; under Claude's 30s hook
                              timeout).
  LONGHOUSE_PERMISSION_HOOK_ENABLED    Set to "0"/"false" to disable the gate.
  LONGHOUSE_PERMISSION_HOOK_FAILMODE   deny (default) | prompt | allow — what to
                              do when the gate is engaged but cannot reach a
                              decision.

SAFETY — this hook can gate a real tool execution, so it must never silently
allow. Two distinct situations:
  * NOT ENGAGED (unconfigured / disabled / no session+tool ids): emit NO decision
    and exit 0 — the gate stays out of the way and Claude runs its own flow.
  * ENGAGED but cannot decide (register fail, poll error, timeout, malformed
    response, unknown decision, uncaught bug): apply LONGHOUSE_PERMISSION_HOOK_FAILMODE,
    default DENY. "prompt" falls back to Claude's native prompt (only safe when
    the session was launched WITHOUT --dangerously-skip-permissions); "allow" is
    an explicit opt-out. The wait is always bounded, so the turn never hangs.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Keep the total hook budget safely under Claude's PreToolUse hook timeout (30s):
# one register + one final poll can each take up to _REQUEST_TIMEOUT_S, so cap the
# wait so register + waiting + a trailing request stays under the budget.
# Set once the gate has committed to producing a decision, so the top-level
# backstop knows whether an uncaught error must fail-deny vs stay out of the way.
_ENGAGED = False

_DEFAULT_TIMEOUT_S = 20.0
_MAX_TIMEOUT_S = 20.0
_POLL_INTERVAL_S = 0.5
_REQUEST_TIMEOUT_S = 5.0


def _not_engaged() -> None:
    """The gate is not engaged for this session (unconfigured/disabled).

    Emit nothing and exit 0 so Claude proceeds with its own permission flow.
    This is NOT a decision — it is the gate staying out of the way for sessions
    that did not opt in.
    """
    sys.exit(0)


def _fail_decision() -> None:
    """The gate IS engaged but could not obtain a decision (error/timeout).

    Apply the configured fail-mode. Default is ``deny`` so an unreachable control
    plane can never silently allow a tool. ``prompt`` falls back to Claude's
    native permission prompt (only safe when the session is launched WITHOUT
    --dangerously-skip-permissions). ``allow`` is an explicit, deliberate opt-out.
    """
    mode = str(os.environ.get("LONGHOUSE_PERMISSION_HOOK_FAILMODE", "deny")).strip().lower()
    if mode == "prompt":
        sys.exit(0)  # no decision -> Claude's native prompt
    if mode == "allow":
        _emit_decision("allow", "Longhouse gate fail-mode=allow")
    _emit_decision("deny", "Longhouse permission gate could not reach a decision")


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


def _post_json(url: str, body: dict, token: str) -> dict | None:
    """POST and return the parsed JSON ack, or None on non-2xx."""
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Agents-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
        if not (200 <= resp.status < 300):
            return None
        return json.loads(resp.read().decode("utf-8") or "{}")


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
    # --- Gate engaged? If not, stay out of the way (no decision). ---
    if not _enabled():
        _not_engaged()

    base_url = str(os.environ.get("LONGHOUSE_HOOK_URL") or "").strip().rstrip("/")
    token = str(os.environ.get("LONGHOUSE_HOOK_TOKEN") or "").strip()
    if not base_url:
        _not_engaged()

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _not_engaged()
        return

    session_id = (
        str(os.environ.get("LONGHOUSE_MANAGED_SESSION_ID") or "").strip()
        or str(hook_input.get("session_id") or "").strip()
    )
    tool_use_id = str(hook_input.get("tool_use_id") or "").strip()
    tool_name = str(hook_input.get("tool_name") or "").strip()
    tool_input = hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    if not session_id or not tool_use_id:
        _not_engaged()

    try:
        timeout_s = float(os.environ.get("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = _DEFAULT_TIMEOUT_S
    # Clamp to a finite budget under Claude's hook timeout — never hang the turn.
    timeout_s = max(0.0, min(timeout_s, _MAX_TIMEOUT_S))

    # --- From here the gate IS engaged: any failure (including an uncaught bug) ---
    # --- applies the fail-mode, which defaults to deny (never silently allow). ---
    global _ENGAGED
    _ENGAGED = True

    # One absolute deadline covers register + all polls so total wall time stays
    # under timeout_s regardless of per-request latency.
    deadline = time.monotonic() + timeout_s

    # 1. Register the held permission request.
    try:
        ack = _post_json(
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
        _fail_decision()
        return
    if not isinstance(ack, dict):
        _fail_decision()
        return

    # 2. Long-poll by the unique pause_request_id from the ack. A missing id means
    # we cannot safely identify our row → fail-deny rather than fall back to a
    # collapse-prone key lookup.
    pause_request_id = str(ack.get("pause_request_id") or "").strip()
    if not pause_request_id:
        _fail_decision()
        return
    decision_url = f"{base_url}/api/agents/permission-decision?" + urllib.parse.urlencode(
        {"session_id": session_id, "tool_use_id": tool_use_id, "pause_request_id": pause_request_id}
    )
    while time.monotonic() < deadline:
        try:
            result = _get_decision(decision_url, token)
        except (urllib.error.URLError, OSError, ValueError):
            _fail_decision()
            return
        if isinstance(result, dict) and result.get("resolved"):
            decision = str(result.get("decision") or "").strip().lower()
            if decision in {"allow", "deny", "ask"}:
                _emit_decision(decision, result.get("reason"))
            # Resolved but with an unrecognized decision → fail safe (deny).
            _fail_decision()
        time.sleep(_POLL_INTERVAL_S)

    # 3. No decision in time → apply fail-mode (default deny).
    _fail_decision()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Backstop against an uncaught bug. If the gate had already engaged, fail
        # to the configured mode (default deny) so a crash can't silently allow;
        # otherwise stay out of the way.
        if _ENGAGED:
            _fail_decision()
        sys.exit(0)
