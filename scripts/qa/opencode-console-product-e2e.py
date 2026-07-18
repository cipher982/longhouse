#!/usr/bin/env python3
"""Real Runtime Host -> Machine Agent -> stock OpenCode Console proof."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

import tomllib


def _defaults() -> tuple[str, str]:
    home = Path.home() / ".longhouse"
    api_url = os.environ.get("LONGHOUSE_API_URL", "").strip()
    config_path = home / "config.toml"
    if not api_url and config_path.exists():
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
        api_url = str((payload.get("shipper") or {}).get("api_url") or "").strip()
    token = os.environ.get("LONGHOUSE_MACHINE_TOKEN", "").strip()
    token_path = home / "machine" / "device-token"
    if not token and token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
    return api_url.rstrip("/"), token


def _request(api_url: str, token: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{api_url}{path}",
        data=body,
        method=method,
        headers={
            "X-Agents-Token": token,
            "Content-Type": "application/json",
            "User-Agent": "longhouse-opencode-console-product-e2e/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail[:1000]}") from error


def _events(api_url: str, token: str, session_id: str) -> list[dict]:
    return list(
        _request(api_url, token, "GET", f"/api/agents/sessions/{session_id}/events?limit=100").get("events") or []
    )


def _wait_for_search(api_url: str, token: str, session_id: str, marker: str, timeout: float = 90) -> None:
    query = urllib.parse.urlencode(
        {
            "provider": "opencode",
            "query": marker,
            "days_back": 1,
            "limit": 5,
            "include_test": "true",
            "hide_autonomous": "false",
        }
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = _request(api_url, token, "GET", f"/api/agents/sessions?{query}")
        if session_id in {str(session.get("id")) for session in payload.get("sessions") or []}:
            return
        time.sleep(2)
    raise RuntimeError("OpenCode Console marker did not converge into session search")


def _wait_for_assistant_marker(
    api_url: str,
    token: str,
    session_id: str,
    marker: str,
    *,
    minimum: int = 1,
    timeout: float = 90,
) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _events(api_url, token, session_id)
        matches = [
            event
            for event in events
            if event.get("role") == "assistant" and marker in str(event.get("content_text") or "")
        ]
        if len(matches) >= minimum:
            return events
        time.sleep(1)
    raise RuntimeError(f"assistant marker {marker} did not archive {minimum} time(s)")


def _start_turn(api_url: str, token: str, session_id: str, message: str, request_id: str) -> dict:
    path = f"/api/agents/sessions/{session_id}/turns"
    payload = {"message": message, "client_request_id": request_id}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = _request(api_url, token, "POST", path, payload)
        if result.get("state") in {"active", "starting", "completed"} and result.get("run_id"):
            return result
        if result.get("state") != "queued":
            raise RuntimeError(f"turn was not accepted: {result}")
        time.sleep(0.5)
    raise RuntimeError("queued Console turn was not assigned a run within 30 seconds")


def _wait_for_tool(run_id: str, timeout: float = 90) -> dict:
    claim_path = Path.home() / ".longhouse" / "agent" / "turn-claims" / f"{run_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if claim_path.exists():
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            stdout_path = Path(str(claim.get("stdout_path") or ""))
            if stdout_path.is_file() and '"type":"tool_use"' in stdout_path.read_text(
                encoding="utf-8", errors="replace"
            ):
                return claim
            if claim.get("state") in {"terminal", "failed"}:
                raise RuntimeError(f"tool turn ended before interruption: {claim}")
        time.sleep(0.25)
    raise RuntimeError("OpenCode did not begin the interrupt canary tool")


def _wait_for_cancel(run_id: str, timeout: float = 20) -> dict:
    claim_path = Path.home() / ".longhouse" / "agent" / "turn-claims" / f"{run_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        if claim.get("state") == "terminal":
            if (claim.get("result") or {}).get("terminal_state") != "run_cancelled":
                raise RuntimeError(f"interrupt settled incorrectly: {claim}")
            pgid = int(claim["process_group_id"])
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return claim
            raise RuntimeError(f"interrupted provider process group {pgid} is still alive")
        time.sleep(0.1)
    raise RuntimeError("interrupted OpenCode turn did not settle")


def run(args: argparse.Namespace) -> dict:
    api_url, token = _defaults()
    api_url = args.api_url.rstrip("/") if args.api_url else api_url
    if not api_url or not token:
        raise RuntimeError("Longhouse API URL and machine token are required")
    marker = f"LH_OPENCODE_PRODUCT_{uuid4().hex}"
    post_cancel_marker = f"LH_OPENCODE_AFTER_CANCEL_{uuid4().hex}"
    created = _request(
        api_url,
        token,
        "POST",
        "/api/agents/sessions",
        {
            "provider": "opencode",
            "device_id": args.device_id,
            "cwd": str(Path(args.cwd).resolve()),
            "project": "opencode-console-product-e2e",
            "display_name": "OpenCode Console product E2E",
            "launch_surface": "product-e2e",
        },
    )
    session_id = str(created["session_id"])
    first = _start_turn(
        api_url,
        token,
        session_id,
        f"Remember {marker}. Reply with exactly {marker} and nothing else.",
        f"product-e2e-{uuid4()}",
    )
    _wait_for_assistant_marker(api_url, token, session_id, marker)
    second = _start_turn(
        api_url,
        token,
        session_id,
        "Reply with exactly the marker from the previous turn and nothing else.",
        f"product-e2e-{uuid4()}",
    )
    events = _wait_for_assistant_marker(api_url, token, session_id, marker, minimum=2)
    second_claim = json.loads(
        (Path.home() / ".longhouse" / "agent" / "turn-claims" / f"{second['run_id']}.json").read_text()
    )
    argv = list((second_claim.get("result") or {}).get("argv") or [])
    if "--session" not in argv:
        raise RuntimeError(f"second OpenCode turn did not use explicit native resume: {argv}")
    tool_turn = _start_turn(
        api_url,
        token,
        session_id,
        "Use the bash tool to run exactly: sleep 60. Do not finish before the command finishes.",
        f"product-e2e-{uuid4()}",
    )
    _wait_for_tool(str(tool_turn["run_id"]))
    interrupted = _request(
        api_url,
        token,
        "POST",
        f"/api/agents/sessions/{session_id}/turns/current/interrupt",
    )
    if interrupted.get("interrupt_dispatched") is not True:
        raise RuntimeError(f"interrupt was not dispatched: {interrupted}")
    cancelled = _wait_for_cancel(str(tool_turn["run_id"]))
    post_cancel = _start_turn(
        api_url,
        token,
        session_id,
        f"Reply with exactly {post_cancel_marker} and nothing else.",
        f"product-e2e-{uuid4()}",
    )
    final_events = _wait_for_assistant_marker(api_url, token, session_id, post_cancel_marker)
    _wait_for_search(api_url, token, session_id, marker)
    result = {
        "status": "pass",
        "provider": "opencode",
        "session_id": session_id,
        "thread_id": created["thread_id"],
        "first_run_id": first["run_id"],
        "resume_run_id": second["run_id"],
        "interrupt_run_id": tool_turn["run_id"],
        "post_cancel_run_id": post_cancel["run_id"],
        "provider_session_id": second_claim.get("provider_thread_id"),
        "resume_argv_has_explicit_session": True,
        "cancel_terminal_state": (cancelled.get("result") or {}).get("terminal_state"),
        "archived_event_count": len(final_events),
        "search_converged": True,
        "resume_marker_count": sum(
            event.get("role") == "assistant" and marker in str(event.get("content_text") or "") for event in events
        ),
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url")
    parser.add_argument("--device-id", default="cinder")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), indent=2))
        return 0
    except Exception as error:  # noqa: BLE001 - CLI must emit one terminal failure
        print(json.dumps({"status": "fail", "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
