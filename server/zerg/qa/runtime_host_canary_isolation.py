"""Reusable Runtime Host isolation receipt for live provider canaries.

Canaries may need to register as an ordinary human launch to prove the actual
product path.  That identity is evidence, not permission to leave factory
rows in user surfaces.  This module hides a completed canary and proves the
four projection boundaries that previously had independent cleanup rules.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

RuntimeRequest = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


def runtime_host_request(
    api_url: str,
    agents_token: str,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = json.dumps(body, sort_keys=True).encode() if body is not None else None
    endpoint = path if path.startswith("/api/") else f"/api/agents/{path.lstrip('/')}"
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{endpoint}",
        headers={
            "X-Agents-Token": agents_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "LonghouseProviderFactory/1.0",
        },
        data=payload,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"Runtime Host HTTP {exc.code}: {detail[:1000]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Runtime Host returned a non-object")
    return result


def _ids(payload: dict[str, Any]) -> set[str]:
    rows = payload.get("sessions")
    if not isinstance(rows, list):
        return set()
    return {str(row.get("id") or row.get("session_id")) for row in rows if isinstance(row, dict)}


def _workspace_paths(payload: dict[str, Any]) -> set[str]:
    rows = payload.get("workspaces")
    if not isinstance(rows, list):
        return set()
    return {str(row.get("path")) for row in rows if isinstance(row, dict) and row.get("path")}


def hide_and_verify_canary_isolation(
    request: RuntimeRequest,
    *,
    session_id: str,
    provider: str,
    project: str,
    device_id: str,
    cwd: str,
    owned_processes_dead: Callable[[], bool],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Hide one row, then prove it cannot leak through adjacent user surfaces.

    Title debt is proven absent by the strongest portable launch-canary fact:
    the retained row has no user turn.  The title reconciler requires
    ``user_messages > 0`` before a row can enter its candidate queue.  A
    future canary that performs a real turn must supply a separate title or a
    dedicated title-debt authority instead of weakening this check.
    """

    hidden = request(f"sessions/{session_id}/timeline-visibility", "PATCH", {"hidden": True})
    query = urllib.parse.urlencode(
        {
            "project": project,
            "provider": provider,
            "device_id": device_id,
            "hide_autonomous": "false",
            "limit": 100,
        }
    )
    open_query = urllib.parse.urlencode({"project": project, "limit": 100})
    workspace_query = urllib.parse.urlencode({"limit": 50, "days_back": 180})
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        direct = request(f"sessions/{session_id}", "GET", None)
        default = request(f"sessions?{query}", "GET", None)
        # The removed /api/agents/sessions/active route depended on the archive
        # database and never served on a real catalogd Runtime Host.  The live
        # timeline is the actual open-session product surface.
        open_sessions = request(f"/api/timeline/sessions?{open_query}", "GET", None)
        workspaces = request(
            f"machines/{urllib.parse.quote(device_id, safe='')}/workspaces?{workspace_query}",
            "GET",
            None,
        )
        axes = {
            "default_timeline_absent": session_id not in _ids(default),
            "open_absent": session_id not in _ids(open_sessions),
            "title_debt_absent": int(direct.get("user_messages") or 0) == 0,
            "workspace_suggestion_absent": cwd not in _workspace_paths(workspaces),
            "direct_retrieval_succeeds": str(direct.get("id") or "") == session_id,
            "owned_processes_dead": owned_processes_dead(),
        }
        last = {
            "status": "pass" if all(axes.values()) else "pending",
            "session_id": session_id,
            "hidden": hidden.get("hidden") is True,
            "axes": axes,
            "title_debt_basis": "storage_title_candidates_requires_user_messages",
            "workspace_path": cwd,
        }
        if last["hidden"] and last["status"] == "pass":
            return last
        time.sleep(0.25)
    return {**last, "status": "fail", "failure_code": "canary_isolation_timeout"}


__all__ = ["hide_and_verify_canary_isolation", "runtime_host_request"]
