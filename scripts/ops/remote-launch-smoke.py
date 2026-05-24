#!/usr/bin/env python3
"""Hosted remote-launch smoke for the browser/iOS launch path.

This deliberately exercises the public browser-authenticated API:

1. wait for /api/health to report the expected build commit
2. mint a browser session cookie from the hosted tenant container
3. pick an online machine that advertises codex.launch
4. POST /api/sessions/launch
5. wait longer than the launch lease
6. POST /api/sessions/{id}/input with a nonce prompt
7. query hosted SQLite and require an assistant-role event containing the nonce
8. best-effort stop the Codex bridge process when the smoke is done

The final assertion is DB-backed because timeline previews can include user
echoes; launch readiness needs to prove the provider answered, not just that
the message was accepted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen


DEFAULT_SUBDOMAIN = "david010"
DEFAULT_PROJECT = "zerg"
DEFAULT_WAIT_AFTER_LAUNCH_SECS = 135
DEFAULT_ASSISTANT_TIMEOUT_SECS = 240
DEFAULT_POLL_INTERVAL_SECS = 5
COOKIE_NAME = "longhouse_session"
CODEX_LAUNCH_CAPABILITY = "codex.launch"
SMOKE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15 LonghouseRemoteLaunchSmoke/1.0"
)


class SmokeError(RuntimeError):
    """Launch smoke failed for an expected, reportable reason."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: str
    json_body: Any


def _json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    cookie: str | None = None,
    timeout: float = 15,
) -> HttpResult:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": SMOKE_USER_AGENT,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = f"{COOKIE_NAME}={cookie}"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return HttpResult(response.status, response_body, _json_loads(response_body))
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return HttpResult(exc.code, response_body, _json_loads(response_body))
    except URLError as exc:
        raise SmokeError(f"{method} {url} failed: {exc}") from exc


def _require_json_object(result: HttpResult, *, context: str) -> dict[str, Any]:
    if result.status < 200 or result.status >= 300:
        raise SmokeError(f"{context} returned HTTP {result.status}: {result.body[:500]}")
    if not isinstance(result.json_body, dict):
        raise SmokeError(f"{context} returned non-object JSON: {result.body[:500]}")
    return result.json_body


def _commit_matches(observed: str | None, expected: str | None) -> bool:
    observed = (observed or "").strip()
    expected = (expected or "").strip()
    if not expected:
        return True
    if not observed:
        return False
    if len(expected) >= 40:
        return observed == expected
    if len(expected) >= 7:
        return observed.startswith(expected)
    return observed == expected


def _health_commit(payload: dict[str, Any]) -> str | None:
    build = payload.get("build")
    if isinstance(build, dict):
        commit = build.get("commit")
        if isinstance(commit, str) and commit.strip():
            return commit.strip()
    return None


def wait_for_health_commit(base_url: str, expected_commit: str | None, *, timeout_secs: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_secs
    last: dict[str, Any] | None = None
    last_status: int | None = None
    last_body = ""
    url = f"{base_url.rstrip('/')}/api/health"
    while time.monotonic() < deadline:
        result = _http_json("GET", url, timeout=15)
        last_status = result.status
        last_body = result.body
        if result.status == 200 and isinstance(result.json_body, dict):
            last = result.json_body
            if _commit_matches(_health_commit(last), expected_commit):
                return last
        time.sleep(5)
    observed = _health_commit(last or {})
    raise SmokeError(
        f"timed out waiting for {url} commit={expected_commit or '<any>'}; "
        f"observed={observed or '<missing>'} last_status={last_status or '<none>'} last_body={last_body[:300]!r}"
    )


def _run_remote_python(
    ssh_target: str,
    *,
    container: str,
    script: str,
    args: list[str] | None = None,
    timeout_secs: int = 30,
) -> subprocess.CompletedProcess[str]:
    remote_command = ["docker", "exec", "-i", container, "python3", "-"]
    if args:
        remote_command.extend(args)
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", ssh_target, shlex.join(remote_command)]
    return subprocess.run(
        command,
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout_secs,
        check=False,
    )


def _parse_last_json_line(output: str) -> Any:
    for line in reversed((output or "").splitlines()):
        parsed = _json_loads(line)
        if parsed is not None:
            return parsed
    return None


def mint_browser_cookie(*, ssh_target: str, container: str) -> str:
    script = r"""
from zerg.auth.session_tokens import _issue_access_token
from zerg.database import db_session
from zerg.models.models import User

with db_session() as db:
    user = db.query(User).order_by(User.id.asc()).first()
    if user is None:
        raise SystemExit("no browser user found")
    print(_issue_access_token(user.id, user.email, display_name=user.display_name, avatar_url=user.avatar_url))
"""
    proc = _run_remote_python(ssh_target, container=container, script=script, timeout_secs=30)
    token = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    if proc.returncode != 0 or not token:
        raise SmokeError(f"could not mint browser cookie: {(proc.stderr or proc.stdout or '').strip()[-500:]}")
    return token


def discover_machine(base_url: str, cookie: str, *, requested_device_id: str | None = None) -> dict[str, Any]:
    result = _http_json("GET", f"{base_url.rstrip('/')}/api/timeline/machines", cookie=cookie, timeout=20)
    payload = _require_json_object(result, context="GET /api/timeline/machines")
    machines = payload.get("machines")
    if not isinstance(machines, list):
        raise SmokeError("machines response missing machines[]")

    eligible = [
        machine
        for machine in machines
        if isinstance(machine, dict)
        and machine.get("online") is True
        and machine.get("can_launch_codex") is True
    ]
    if requested_device_id:
        for machine in eligible:
            if str(machine.get("device_id") or "") == requested_device_id:
                return machine
        visible = [str(machine.get("device_id") or "") for machine in machines if isinstance(machine, dict)]
        raise SmokeError(f"requested device_id={requested_device_id!r} is not online with codex.launch; visible={visible}")
    if not eligible:
        visible = [
            {
                "device_id": machine.get("device_id"),
                "online": machine.get("online"),
                "supports": machine.get("supports"),
                "launch_blocked_by": machine.get("launch_blocked_by"),
            }
            for machine in machines
            if isinstance(machine, dict)
        ]
        raise SmokeError(f"no online codex-launch-capable machine found; visible={visible}")
    return eligible[0]


def launch_session(
    base_url: str,
    cookie: str,
    *,
    device_id: str,
    cwd: str,
    project: str,
    display_name: str,
    client_request_id: str,
) -> dict[str, Any]:
    payload = {
        "device_id": device_id,
        "provider": "codex",
        "cwd": cwd,
        "project": project,
        "display_name": display_name,
        "client_request_id": client_request_id,
    }
    result = _http_json("POST", f"{base_url.rstrip('/')}/api/sessions/launch", body=payload, cookie=cookie, timeout=60)
    data = _require_json_object(result, context="POST /api/sessions/launch")
    session_id = str(data.get("session_id") or "")
    if not session_id:
        raise SmokeError(f"launch response missing session_id: {data}")
    state = str(data.get("launch_state") or "")
    if state != "live":
        raise SmokeError(f"launch failed state={state} code={data.get('launch_error_code')} message={data.get('launch_error_message')}")
    return data


def send_session_input(base_url: str, cookie: str, *, session_id: str, text: str, client_request_id: str) -> dict[str, Any]:
    payload = {"text": text, "intent": "auto", "client_request_id": client_request_id}
    result = _http_json("POST", f"{base_url.rstrip('/')}/api/sessions/{session_id}/input", body=payload, cookie=cookie, timeout=60)
    return _require_json_object(result, context=f"POST /api/sessions/{session_id}/input")


def send_nonce_prompt(base_url: str, cookie: str, *, session_id: str, nonce: str, client_request_id: str) -> dict[str, Any]:
    text = (
        "Remote launch smoke check. Reply with exactly this token and no extra analysis: "
        f"{nonce}"
    )
    return send_session_input(base_url, cookie, session_id=session_id, text=text, client_request_id=client_request_id)


def send_second_input_probe(base_url: str, cookie: str, *, session_id: str, nonce: str, client_request_id: str) -> dict[str, Any]:
    text = f"Second input race probe for remote launch smoke {nonce}. Acknowledge briefly."
    response = send_session_input(base_url, cookie, session_id=session_id, text=text, client_request_id=client_request_id)
    outcome = str(response.get("outcome") or "")
    if outcome not in {"sent", "queued"}:
        raise SmokeError(f"second input probe returned unexpected outcome={outcome!r}: {response}")
    return response


def hosted_session_debug(*, ssh_target: str, container: str, session_id: str, limit: int = 30) -> dict[str, Any]:
    script = r"""
import json, sqlite3, sys

sid, limit = sys.argv[1], int(sys.argv[2])
from zerg.config import get_settings

settings = get_settings()
path = settings.database_url
if path.startswith("sqlite:///"):
    path = path.removeprefix("sqlite:///")
elif path.startswith("sqlite:////"):
    path = "/" + path.removeprefix("sqlite:////")
else:
    raise SystemExit(f"unsupported database_url for smoke: {settings.database_url}")

conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

def table(name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def rows(sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def one(sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None

payload = {"session_id": sid}
if table("sessions"):
    payload["session"] = one("SELECT id, provider, project, device_id, cwd, started_at, ended_at, last_activity_at, user_messages, assistant_messages, tool_calls, provider_session_id, launch_state FROM sessions WHERE id=? OR provider_session_id=?", (sid, sid))
if table("session_runtime_state"):
    payload["runtime_state"] = one("SELECT session_id, lifecycle, phase, progress, terminal_state, updated_at, last_observed_at, active_turn_id, pending_input_count FROM session_runtime_state WHERE session_id=? ORDER BY updated_at DESC LIMIT 1", (sid,))
if table("events"):
    payload["event_stats"] = one("SELECT count(*) AS count, min(timestamp) AS first_timestamp, max(timestamp) AS last_timestamp FROM events WHERE session_id=?", (sid,))
    payload["recent_events"] = rows("SELECT id, role, tool_name, substr(coalesce(content_text, tool_output_text, ''), 1, 1000) AS text, timestamp FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit))
    payload["assistant_events"] = rows("SELECT id, role, tool_name, substr(coalesce(content_text, tool_output_text, ''), 1, 1000) AS text, timestamp FROM events WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT ?", (sid, limit))
if table("session_inputs"):
    payload["recent_inputs"] = rows("SELECT id, intent, status, last_error, created_at, updated_at FROM session_inputs WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit))
print(json.dumps(payload, default=str))
"""
    proc = _run_remote_python(
        ssh_target,
        container=container,
        script=script,
        args=[session_id, str(limit)],
        timeout_secs=30,
    )
    parsed = _parse_last_json_line(proc.stdout or "")
    if proc.returncode != 0 or not isinstance(parsed, dict):
        raise SmokeError(f"could not inspect hosted session: {(proc.stderr or proc.stdout or '').strip()[-700:]}")
    return parsed


def assistant_events_contain(data: dict[str, Any], text: str) -> bool:
    events = data.get("assistant_events") or data.get("recent_events") or []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("role") or "") != "assistant":
            continue
        if text in str(event.get("text") or ""):
            return True
    return False


def poll_for_assistant_nonce(
    *,
    ssh_target: str,
    container: str,
    session_id: str,
    nonce: str,
    timeout_secs: int,
    interval_secs: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_secs
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = hosted_session_debug(ssh_target=ssh_target, container=container, session_id=session_id)
        if assistant_events_contain(last, nonce):
            return last
        time.sleep(interval_secs)
    recent = (last or {}).get("recent_events") or []
    raise SmokeError(f"timed out waiting for assistant event containing {nonce}; recent_events={recent[:5]}")


def stop_codex_bridge(session_id: str, *, target_ssh: str | None = None) -> dict[str, Any]:
    command = ["longhouse-engine", "codex-bridge", "stop", "--session-id", session_id, "--reason", "user_closed"]
    if target_ssh:
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", target_ssh, shlex.join(command)]
    started = time.monotonic()
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)
    except FileNotFoundError:
        return {"ok": False, "error": "longhouse-engine not found", "cmd": command}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "longhouse-engine codex-bridge stop timed out", "cmd": command}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout": (proc.stdout or "")[-500:],
        "stderr": (proc.stderr or "")[-500:],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("REMOTE_LAUNCH_SMOKE_BASE_URL"))
    parser.add_argument("--subdomain", default=os.environ.get("REMOTE_LAUNCH_SMOKE_SUBDOMAIN", DEFAULT_SUBDOMAIN))
    parser.add_argument("--container", default=os.environ.get("REMOTE_LAUNCH_SMOKE_CONTAINER"))
    parser.add_argument("--ssh-target", default=os.environ.get("REMOTE_LAUNCH_SMOKE_SSH_TARGET", "zerg"))
    parser.add_argument("--bridge-stop-ssh-target", default=os.environ.get("REMOTE_LAUNCH_SMOKE_BRIDGE_STOP_SSH_TARGET"))
    parser.add_argument("--device-id", default=os.environ.get("REMOTE_LAUNCH_SMOKE_DEVICE_ID"))
    parser.add_argument("--cwd", default=os.environ.get("REMOTE_LAUNCH_SMOKE_CWD"))
    parser.add_argument("--project", default=os.environ.get("REMOTE_LAUNCH_SMOKE_PROJECT", DEFAULT_PROJECT))
    parser.add_argument("--expected-commit", default=os.environ.get("REMOTE_LAUNCH_SMOKE_EXPECTED_COMMIT") or os.environ.get("GITHUB_SHA"))
    parser.add_argument("--health-timeout-secs", type=int, default=int(os.environ.get("REMOTE_LAUNCH_SMOKE_HEALTH_TIMEOUT_SECS", "600")))
    parser.add_argument("--wait-after-launch-secs", type=int, default=int(os.environ.get("REMOTE_LAUNCH_SMOKE_WAIT_AFTER_LAUNCH_SECS", str(DEFAULT_WAIT_AFTER_LAUNCH_SECS))))
    parser.add_argument("--assistant-timeout-secs", type=int, default=int(os.environ.get("REMOTE_LAUNCH_SMOKE_ASSISTANT_TIMEOUT_SECS", str(DEFAULT_ASSISTANT_TIMEOUT_SECS))))
    parser.add_argument("--poll-interval-secs", type=int, default=int(os.environ.get("REMOTE_LAUNCH_SMOKE_POLL_INTERVAL_SECS", str(DEFAULT_POLL_INTERVAL_SECS))))
    parser.add_argument("--output-json", default=os.environ.get("REMOTE_LAUNCH_SMOKE_OUTPUT_JSON"))
    parser.add_argument("--skip-stop", action="store_true", default=os.environ.get("REMOTE_LAUNCH_SMOKE_SKIP_STOP") == "1")
    return parser.parse_args(argv)


def _write_output(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = (args.base_url or f"https://{args.subdomain}.longhouse.ai").rstrip("/")
    container = args.container or f"longhouse-{args.subdomain}"
    cwd = str(args.cwd or "").strip()
    if not cwd:
        raise SmokeError("REMOTE_LAUNCH_SMOKE_CWD/--cwd is required because cwd is resolved on the target machine")
    if not cwd.startswith("/"):
        raise SmokeError(f"cwd must be absolute on the target machine: {cwd!r}")
    run_id = os.environ.get("GITHUB_RUN_ID") or time.strftime("%Y%m%d%H%M%S")
    nonce = f"LH_REMOTE_LAUNCH_SMOKE_{run_id}_{uuid.uuid4().hex[:8]}"
    client_prefix = re.sub(r"[^a-zA-Z0-9_.:-]", "-", f"remote-smoke-{run_id}-{uuid.uuid4().hex[:8]}")[:48]
    session_id: str | None = None
    result: dict[str, Any] = {
        "ok": False,
        "base_url": base_url,
        "subdomain": args.subdomain,
        "container": container,
        "ssh_target": args.ssh_target,
        "bridge_stop_ssh_target": args.bridge_stop_ssh_target,
        "project": args.project,
        "cwd": cwd,
        "expected_commit": args.expected_commit,
        "nonce": nonce,
        "hostname": socket.gethostname(),
    }

    try:
        health = wait_for_health_commit(base_url, args.expected_commit, timeout_secs=args.health_timeout_secs)
        result["health"] = {"status": health.get("status"), "commit": _health_commit(health), "build": health.get("build")}

        cookie = mint_browser_cookie(ssh_target=args.ssh_target, container=container)
        result["auth"] = {"browser_cookie_minted": True}

        machine = discover_machine(base_url, cookie, requested_device_id=args.device_id)
        result["machine"] = {
            "device_id": machine.get("device_id"),
            "machine_name": machine.get("machine_name"),
            "engine_build": machine.get("engine_build"),
            "supports": machine.get("supports"),
        }

        display_name = f"remote-smoke-{run_id}"
        launch_response = launch_session(
            base_url,
            cookie,
            device_id=str(machine["device_id"]),
            cwd=cwd,
            project=args.project,
            display_name=display_name,
            client_request_id=f"{client_prefix}-launch",
        )
        session_id = str(launch_response["session_id"])
        result["session_id"] = session_id
        result["launch_response"] = launch_response
        result["launch"] = hosted_session_debug(ssh_target=args.ssh_target, container=container, session_id=session_id)

        time.sleep(args.wait_after_launch_secs)
        send_response = send_nonce_prompt(
            base_url,
            cookie,
            session_id=session_id,
            nonce=nonce,
            client_request_id=f"{client_prefix}-input",
        )
        result["send"] = send_response
        result["second_send"] = send_second_input_probe(
            base_url,
            cookie,
            session_id=session_id,
            nonce=nonce,
            client_request_id=f"{client_prefix}-second",
        )

        observed = poll_for_assistant_nonce(
            ssh_target=args.ssh_target,
            container=container,
            session_id=session_id,
            nonce=nonce,
            timeout_secs=args.assistant_timeout_secs,
            interval_secs=args.poll_interval_secs,
        )
        result["observed"] = observed
        result["ok"] = True
    except SmokeError as exc:
        result["error"] = str(exc)
    finally:
        if session_id and not args.skip_stop:
            result["cleanup"] = stop_codex_bridge(session_id, target_ssh=args.bridge_stop_ssh_target)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    payload: dict[str, Any]
    try:
        payload = run(args)
    except Exception as exc:
        payload = {"ok": False, "error": f"unexpected error: {exc}"}
        _write_output(args.output_json, payload)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    _write_output(args.output_json, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
