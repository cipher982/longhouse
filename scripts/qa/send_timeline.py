#!/usr/bin/env python3
"""Watch a send happen, step by step, against a disposable local Longhouse.

Boots a scratch Runtime Host (AUTH_DISABLED, loopback only), a second Machine
Agent under a scratch HOME with stock Codex, a relay that can cut the client
off (to model a deploy restart), and the web UI; creates a Console session;
then drives the real UI with e2e/tools/send-timeline.mjs and prints one merged
timeline: bubble state, activity headline, input POSTs, server receipts.

  python3 scripts/qa/send_timeline.py run single     # up, run, down
  python3 scripts/qa/send_timeline.py run midtool    # send while a tool runs
  python3 scripts/qa/send_timeline.py run restart    # send while the host is unreachable
  python3 scripts/qa/send_timeline.py up | down      # keep the stack for iteration

The run exits non-zero unless every send was accepted, stopped saying
"Sending…" once accepted, and never ended "Not delivered".

Codex runs with a copy of ~/.codex/auth.json in the scratch HOME; its
transcripts stay in that HOME, which the real Machine Agent never watches.
Everything lives under /tmp/agents/send-timeline and is removed by `down`.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path("/tmp/agents/send-timeline")
STATE = SCRATCH / "state.json"
DEVICE = "send-timeline-mac"
SCENARIOS = {"single", "midturn", "midtool", "restart", "steer"}


def free_port() -> int:
    # Fresh ports per `up`: a just-stopped stack leaves its ports in TIME_WAIT,
    # which the server's own preflight reads as "already in use".
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def http(method: str, url: str, body: dict | None = None, timeout: float = 5) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def wait_for(label: str, check, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if result := check():
                return result
        except Exception:
            pass
        time.sleep(0.5)
    sys.exit(f"timed out waiting for {label}")


def start(key: str, command: list[str], env: dict, log: str, cwd: Path, state: dict) -> None:
    with open(SCRATCH / log, "ab") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    state[key] = process.pid
    STATE.write_text(json.dumps(state))


def up() -> dict:
    if STATE.exists():
        down()
    try:
        return _up()
    except BaseException:
        down()
        raise


def _up() -> dict:
    # SEND_TIMELINE_ENGINE runs a freshly built engine instead of the installed one.
    engine = os.environ.get("SEND_TIMELINE_ENGINE") or shutil.which("longhouse-engine") or "longhouse-engine"
    for tool in ("longhouse", engine, "codex", "bun", "node"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    codex_auth = Path.home() / ".codex" / "auth.json"
    if not codex_auth.exists():
        sys.exit("~/.codex/auth.json is missing; Codex cannot run in the scratch HOME")
    home = SCRATCH / "home"
    project = SCRATCH / "project"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy(codex_auth, home / ".codex" / "auth.json")
    os.chmod(home / ".codex" / "auth.json", 0o600)
    (home / ".codex" / "config.toml").write_text('model = "gpt-5.6-luna"\nmodel_reasoning_effort = "low"\n')
    (project / "README.md").write_text("# scratch project for send-timeline\n")
    started = time.monotonic()
    server_port, relay_port, web_port = free_port(), free_port(), free_port()
    state: dict = {"scratch": str(SCRATCH), "ports": [server_port, relay_port, web_port]}

    server_env = {
        **os.environ,
        "ENVIRONMENT": "test:e2e",
        "AUTH_DISABLED": "1",
        "LLM_DISABLED": "1",
        "DATABASE_URL": f"sqlite:///{SCRATCH / 'longhouse.db'}",
        "JWT_SECRET": "send-timeline-jwt",
        "FERNET_SECRET": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "INTERNAL_API_SECRET": "send-timeline-internal",
    }
    start(
        "server_pid",
        ["uv", "run", "python", "-m", "zerg.cli.main", "serve", "--host", "127.0.0.1", "--port", str(server_port)],
        server_env, "server.log", ROOT / "server", state,
    )
    api = f"http://127.0.0.1:{server_port}"
    wait_for("runtime host (see server.log)", lambda: http("GET", f"{api}/api/health", timeout=2), 120)
    token = wait_for(
        "device token",
        lambda: http("POST", f"{api}/api/devices/tokens", {"name": "send-timeline", "device_id": DEVICE}).get("token"),
        90,
    )

    agent_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "HOME": str(home),
        "LONGHOUSE_HOME": str(home / ".longhouse"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "RUST_LOG": "info",
    }
    subprocess.run(
        ["longhouse", "auth", "--url", api, "--device", DEVICE],
        env={**agent_env, "LONGHOUSE_DEVICE_TOKEN": token}, check=True, capture_output=True,
    )
    start(
        "engine_pid",
        [engine, "connect", "--url", api, "--token", token, "--db", str(SCRATCH / "agent.db"),
         "--machine-name", DEVICE, "--fallback-scan-secs", "2", "--spool-replay-secs", "1"],
        agent_env, "engine.log", project, state,
    )
    wait_for("machine agent", lambda: "control/ws accepted" in (SCRATCH / "server.log").read_text(errors="ignore"), 60)

    offline = SCRATCH / "offline"
    offline.unlink(missing_ok=True)
    start(
        "relay_pid",
        [sys.executable, str(ROOT / "scripts/qa/simlab_proxy.py"), "--listen-port", str(relay_port),
         "--upstream-port", str(server_port), "--offline-file", str(offline)],
        dict(os.environ), "relay.log", ROOT, state,
    )
    relay = f"127.0.0.1:{relay_port}"
    start(
        "web_pid",
        ["bunx", "vite", "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"],
        {**os.environ, "VITE_PROXY_TARGET": f"http://{relay}", "VITE_WS_BASE_URL": f"ws://{relay}", "VITE_AUTH_ENABLED": "false"},
        "web.log", ROOT / "web", state,
    )
    web = f"http://127.0.0.1:{web_port}"
    wait_for("web ui", lambda: http("GET", f"{web}/api/health", timeout=2), 60)

    session = wait_for(
        "console session",
        lambda: http("POST", f"{api}/api/sessions/console", {
            "device_id": DEVICE, "provider": "codex", "cwd": str(project), "launch_surface": "web",
        }, timeout=15).get("session_id"),
        60,
    )
    state.update({"api": api, "web": web, "session_id": session, "offline_file": str(offline)})
    STATE.write_text(json.dumps(state))
    print(f"send-timeline stack up in {time.monotonic() - started:.1f}s: web {web}/timeline/{session}")
    return state


def down() -> None:
    state: dict = {}
    if STATE.exists():
        state = json.loads(STATE.read_text())
        for key in ("web_pid", "relay_pid", "engine_pid", "server_pid"):
            pid = state.get(key)
            if not pid:
                continue
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:  # gone, or a reaped group macOS reports as EPERM
                continue
        time.sleep(2)
        for key in ("web_pid", "relay_pid", "engine_pid", "server_pid"):
            pid = state.get(key)
            try:
                if pid:
                    os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
    # Children the process groups did not cover (catalogd/searchd reparent).
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(
        subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True).stdout
        for port in state.get("ports", [])
    ):
        time.sleep(0.3)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("send-timeline stack down; scratch removed")


def run(scenario: str, keep: bool) -> int:
    if scenario not in SCENARIOS:
        sys.exit(f"scenario must be one of {sorted(SCENARIOS)}")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "session_id" not in state:  # absent, or a partial `up` that failed
        state = up()
    offline = state["offline_file"]
    command = [
        "node", str(ROOT / "e2e/tools/send-timeline.mjs"),
        "--web", state["web"], "--api", state["api"], "--session", state["session_id"],
        "--scenario", scenario, "--out", str(SCRATCH / f"timeline-{scenario}.jsonl"),
        "--down", f"touch {offline}", "--up", f"rm -f {offline}",
    ]
    started = time.monotonic()
    try:
        return subprocess.run(command, cwd=ROOT / "e2e").returncode
    finally:
        print(f"scenario {scenario} took {time.monotonic() - started:.1f}s")
        if not keep:
            down()


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in {"up", "down", "run"}:
        print(__doc__)
        return 2
    if args[0] == "up":
        up()
        return 0
    if args[0] == "down":
        down()
        return 0
    return run(args[1] if len(args) > 1 else "single", keep="--keep" in args)


if __name__ == "__main__":
    sys.exit(main())
