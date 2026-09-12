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

from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.provider_console_lifecycle import _wait_served_run_retirement
from zerg.services.session_title import is_resume_seed_marker
from zerg.services.session_visibility_policy import SessionVisibilityFacts
from zerg.services.session_visibility_policy import evaluate_origin_visibility

RuntimeRequest = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


def runtime_host_request(
    api_url: str,
    agents_token: str,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = json.dumps(body, sort_keys=True).encode() if body is not None else None
    endpoint = f"/api/agents/{path.lstrip('/')}"
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


def _ids(payload: dict[str, Any]) -> set[str] | None:
    rows = payload.get("sessions")
    if not isinstance(rows, list):
        return None
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        value = row.get("id") or row.get("session_id")
        if not isinstance(value, str) or not value.strip():
            return None
        ids.add(value)
    return ids


def _workspace_paths(payload: dict[str, Any]) -> set[str] | None:
    rows = payload.get("workspaces")
    if not isinstance(rows, list):
        return None
    paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not row["path"].strip():
            return None
        paths.add(row["path"])
    return paths


def hide_and_verify_canary_isolation(
    request: RuntimeRequest,
    *,
    session_id: str,
    run_id: str,
    provider: str,
    project: str,
    api_url: str,
    agents_token: str,
    device_id: str,
    cwd: str,
    owned_processes_dead: Callable[[], bool],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Hide one row, then prove it cannot leak through adjacent user surfaces.

    Title debt is absent when the row has no user turn, already has its frozen
    title, or contains the product's explicit Resume seed marker. These are the
    same sufficient facts that keep a row out of the storage title queue.
    """

    served_run_inventory: dict[str, Any]
    if not run_id.strip():
        served_run_inventory = {
            "retired": False,
            "active_run_count": None,
            "session_id": session_id,
            "expected_run_id": None,
            "error": "run_id_unavailable",
        }
    else:
        try:
            served_run_inventory = dict(
                _wait_served_run_retirement(
                    api_url,
                    agents_token,
                    session_id,
                    [{"state": "terminal", "session_id": session_id, "run_id": run_id}],
                    timeout=timeout_seconds,
                )
            )
        except Exception as exc:  # noqa: BLE001 - cleanup evidence must fail closed
            served_run_inventory = {
                "retired": False,
                "active_run_count": None,
                "session_id": session_id,
                "expected_run_id": run_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
    session_retirement = retire_qualification_session(
        api_url,
        agents_token,
        session_id,
        provider=provider,
        project=project,
    )
    served_run_retired = (
        served_run_inventory.get("retired") is True
        and served_run_inventory.get("session_id") == session_id
        and served_run_inventory.get("active_run_count") == 0
    )
    canary_session_hidden = (
        session_retirement.get("status") == "pass"
        and session_retirement.get("session_id") == session_id
        and session_retirement.get("hidden") is True
        and session_retirement.get("archived") is True
        and session_retirement.get("present_in_served_inventory") is False
    )
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
        # The canonical machine list is a flat projection of the same live
        # catalog timeline served to the browser.  Read it with the factory's
        # machine credential so this assertion exercises a route that accepts
        # X-Agents-Token and exposes session ids directly.
        open_sessions = request(f"sessions?{open_query}", "GET", None)
        workspaces = request(
            f"machines/{urllib.parse.quote(device_id, safe='')}/workspaces?{workspace_query}",
            "GET",
            None,
        )
        default_ids = _ids(default)
        open_ids = _ids(open_sessions)
        workspace_paths = _workspace_paths(workspaces)
        raw_user_messages = direct.get("user_messages")
        user_messages = raw_user_messages if type(raw_user_messages) is int and raw_user_messages >= 0 else None
        anchor_title = str(direct.get("anchor_title") or "").strip()
        first_user_message = str(direct.get("first_user_message_preview") or "")
        title_origin_eligible = evaluate_origin_visibility(
            SessionVisibilityFacts(
                provider=provider,
                project=project,
                cwd=cwd,
                machine_id=device_id,
            )
        ).title_origin_eligible
        if not title_origin_eligible:
            title_debt_basis = "origin_ineligible"
        elif user_messages is None:
            title_debt_basis = "user_message_count_unavailable"
        elif user_messages == 0:
            title_debt_basis = "no_user_messages"
        elif anchor_title:
            title_debt_basis = "anchor_title_present"
        elif is_resume_seed_marker(first_user_message):
            title_debt_basis = "resume_seed_marker"
        else:
            title_debt_basis = "storage_title_candidate"
        axes = {
            "default_timeline_absent": default_ids is not None and session_id not in default_ids,
            "open_absent": open_ids is not None and session_id not in open_ids,
            "title_debt_absent": title_debt_basis
            in {"origin_ineligible", "no_user_messages", "anchor_title_present", "resume_seed_marker"},
            "workspace_suggestion_absent": workspace_paths is not None and cwd not in workspace_paths,
            "direct_retrieval_succeeds": str(direct.get("id") or "") == session_id,
            "owned_processes_dead": owned_processes_dead() is True,
        }
        last = {
            "status": (
                "pass" if all(value is True for value in axes.values()) and served_run_retired and canary_session_hidden else "pending"
            ),
            "session_id": session_id,
            "hidden": session_retirement.get("hidden") is True,
            "axes": axes,
            "title_debt_basis": title_debt_basis,
            "workspace_path": cwd,
            "served_run_inventory": served_run_inventory,
            "served_run_retired": served_run_retired,
            "session_retirement": session_retirement,
            "canary_session_hidden": canary_session_hidden,
        }
        if last["status"] == "pass":
            return last
        time.sleep(0.25)
    return {**last, "status": "fail", "failure_code": "canary_isolation_timeout"}


__all__ = ["hide_and_verify_canary_isolation", "runtime_host_request"]
