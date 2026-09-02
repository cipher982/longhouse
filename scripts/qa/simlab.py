#!/usr/bin/env python3
"""simlab: a scripted session through the real pipeline, for the simulator lane.

A scratch HOME holds a Claude transcript root; a real Runtime Host runs on a
scratch SQLite database with auth disabled; a real Machine Agent watches the
root and ships what appears there. This driver plays the provider: it appends
transcript lines at a controlled cadence, with adversarial modes, so the
server projects and the iOS app in the Simulator receives real SSE frames.
Nothing here mocks Longhouse.

    simlab.py up                      start server + engine in a scratch root
    simlab.py play [--turns N] [--cadence-ms MS] [--mode MODE ...]
                                      append a synthetic Claude session
    simlab.py sim [--deploy]          launch the simulator app on the played session
    simlab.py verdict [--since 3m]    verdict envelope from the app's own log
    simlab.py down                    stop server + engine (artifacts stay)

State lives in artifacts/simlab/current/simlab.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "artifacts" / "simlab" / "current"
STATE_FILE = RUN_DIR / "simlab.json"
PROJECT_CWD = "/tmp/simlab-project"
DEVICE_ID = "22222222-2222-4222-8222-222222222222"

MODES = {
    "normal": "one line per cadence tick",
    "burst": "the whole turn at once, then the cadence",
    "delayed-first": "hold the first assistant line for five cadences",
    "split-lines": "write each line in two chunks with a pause between them",
    "malformed": "inject a non-JSON line and an unknown-type entry mid-turn",
    "abandon-resend": "the user sends, escapes, and resends: two sibling user entries",
}


def log(msg: str) -> None:
    print(f"[simlab] {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> None:
    log(msg)
    sys.exit(1)


def load_state() -> dict:
    if not STATE_FILE.exists():
        die("no scratch run; start one with `simlab.py up`")
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: dict | None = None, token: str | None = None, timeout: float = 10) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("content-type", "application/json")
    if token:
        request.add_header("X-Agents-Token", token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def wait_for(description: str, predicate, timeout_s: float, interval_s: float = 0.25):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception:  # noqa: BLE001 - readiness probes fail until they succeed
            result = None
        if result:
            return result
        time.sleep(interval_s)
    die(f"timed out waiting for {description}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# up / down
# --------------------------------------------------------------------------


def built_binary(name: str) -> Path:
    for profile in ("release", "ci"):
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts/build/cargo.py"), "artifact", "--profile", profile, "--bin", name],
            capture_output=True,
            text=True,
            check=False,
        )
        candidate = Path(out.stdout.strip())
        if out.returncode == 0 and candidate.is_file():
            return candidate
    die(f"no built {name}; run `make test-engine` once to build it")


def cmd_up(args: argparse.Namespace) -> None:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if any(alive(pid) for pid in (state.get("server_pid", 0), state.get("engine_pid", 0))):
            die("a scratch run is still up; `simlab.py down` first")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Short on purpose: the engine binds Unix sockets under the scratch home
    # and macOS caps socket paths at 104 bytes.
    scratch = Path("/tmp/longhouse-simlab") / stamp
    home = scratch / "home"
    projects = home / ".claude" / "projects" / PROJECT_CWD.replace("/", "-")
    projects.mkdir(parents=True)
    (home / ".longhouse").mkdir()
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    port = args.port or free_port()
    base_url = f"http://127.0.0.1:{port}"
    fernet = subprocess.run(
        [sys.executable, "-c", "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    server_log = open(scratch / "server.log", "w")
    server = subprocess.Popen(
        ["uv", "run", "python", "-m", "zerg.cli.main", "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT / "server",
        env={
            **os.environ,
            "AUTH_DISABLED": "1",
            "LLM_DISABLED": "1",
            "LOG_LEVEL": "INFO",
            "DATABASE_URL": f"sqlite:///{scratch / 'longhouse.db'}",
            "JWT_SECRET": "simlab-jwt-secret",
            "FERNET_SECRET": fernet,
            "INTERNAL_API_SECRET": "simlab-internal-secret",
        },
        stdout=server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log(f"runtime host starting on {base_url} (pid {server.pid})")
    wait_for("runtime host health", lambda: http("GET", f"{base_url}/api/health", timeout=2), timeout_s=90)

    token = http("POST", f"{base_url}/api/devices/tokens", {"name": "simlab", "device_id": DEVICE_ID})["token"]
    if not token.startswith("zdt_"):
        die(f"expected a device token, got {token[:8]}")

    agent_env = {**os.environ, "HOME": str(home), "LONGHOUSE_HOME": str(home / ".longhouse"), "RUST_LOG": "info"}
    # `auth` records the machine identity the storage handshake later checks
    # against; the machine name must be the device id the token was minted for.
    subprocess.run(
        [str(built_binary("longhouse")), "auth", "--url", base_url],
        env={**agent_env, "LONGHOUSE_DEVICE_TOKEN": token},
        check=True,
        capture_output=True,
        text=True,
    )
    engine_log = open(scratch / "engine.log", "w")
    engine = subprocess.Popen(
        [
            str(built_binary("longhouse-engine")),
            "connect",
            "--url", base_url,
            "--token", token,
            "--db", str(scratch / "agent.db"),
            "--machine-name", DEVICE_ID,
            "--fallback-scan-secs", "2",
            "--spool-replay-secs", "1",
        ],
        env=agent_env,
        stdout=engine_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log(f"machine agent starting (pid {engine.pid}), watching {projects}")

    state = {
        "scratch": str(scratch),
        "home": str(home),
        "projects": str(projects),
        "base_url": base_url,
        "token": token,
        "server_pid": server.pid,
        "engine_pid": engine.pid,
        "started_at": stamp,
    }
    save_state(state)
    wait_for(
        "machine agent control channel",
        lambda: "control/ws" in (scratch / "server.log").read_text() and "accepted" in (scratch / "server.log").read_text(),
        timeout_s=60,
    )
    log("up")
    print(json.dumps({k: state[k] for k in ("base_url", "token", "scratch")}))


def cmd_down(_: argparse.Namespace) -> None:
    if not STATE_FILE.exists():
        log("nothing to stop")
        return
    state = json.loads(STATE_FILE.read_text())
    for key in ("engine_pid", "server_pid"):
        pid = state.get(key, 0)
        if pid and alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                os.kill(pid, signal.SIGTERM)
    time.sleep(1)
    for key in ("engine_pid", "server_pid"):
        pid = state.get(key, 0)
        if pid and alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
    log(f"down; artifacts in {state.get('scratch')}")


# --------------------------------------------------------------------------
# play
# --------------------------------------------------------------------------


class Transcript:
    """Builds Claude Code JSONL entries with correct parent links."""

    def __init__(self, session_id: str, start: datetime) -> None:
        self.session_id = session_id
        self.clock = start
        self.last_uuid: str | None = None

    def _base(self, kind: str, parent: str | None, seconds: float) -> dict:
        self.clock += timedelta(seconds=seconds)
        return {
            "parentUuid": parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": PROJECT_CWD,
            "sessionId": self.session_id,
            "version": "2.0.76",
            "gitBranch": "main",
            "type": kind,
            "uuid": str(uuid.uuid4()),
            "timestamp": self.clock.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }

    def user(self, text: str, parent: str | None = "chain", seconds: float = 2.0) -> dict:
        entry = self._base("user", self.last_uuid if parent == "chain" else parent, seconds)
        entry["message"] = {"role": "user", "content": text}
        self.last_uuid = entry["uuid"]
        return entry

    def assistant_text(self, text: str, seconds: float = 1.5) -> dict:
        entry = self._base("assistant", self.last_uuid, seconds)
        entry["message"] = {
            "model": "claude-sonnet-4-5",
            "id": f"msg_{secrets.token_hex(6)}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 120, "output_tokens": len(text.split())},
        }
        self.last_uuid = entry["uuid"]
        return entry

    def tool_call(self, name: str, tool_input: dict, seconds: float = 1.0) -> tuple[dict, str]:
        entry = self._base("assistant", self.last_uuid, seconds)
        call_id = f"toolu_{secrets.token_hex(8)}"
        entry["message"] = {
            "model": "claude-sonnet-4-5",
            "id": f"msg_{secrets.token_hex(6)}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 80, "output_tokens": 30},
        }
        self.last_uuid = entry["uuid"]
        return entry, call_id

    def tool_result(self, call_id: str, output: str, seconds: float = 0.8) -> dict:
        entry = self._base("user", self.last_uuid, seconds)
        entry["message"] = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": output}],
        }
        entry["toolUseResult"] = {"stdout": output, "stderr": "", "interrupted": False}
        self.last_uuid = entry["uuid"]
        return entry


def synthetic_turns(transcript: Transcript, turns: int, modes: set[str]) -> list[dict | str]:
    """Entries (or raw lines for malformed mode) for N user turns."""
    entries: list[dict | str] = []
    for turn in range(1, turns + 1):
        prompt = f"Turn {turn}: list the files in the project and summarise what changed."
        if "abandon-resend" in modes and turn == 2:
            anchor = transcript.last_uuid
            entries.append(transcript.user(prompt, seconds=3.0))
            # Escape-and-resend: a sibling of the first send, same parent.
            entries.append(transcript.user(prompt + " Also check the tests.", parent=anchor, seconds=14.0))
        else:
            entries.append(transcript.user(prompt))
        entries.append(transcript.assistant_text(f"Looking at turn {turn} now. I'll list the tree first."))
        call, call_id = transcript.tool_call("Bash", {"command": "ls -la", "description": "List files"})
        entries.append(call)
        if "malformed" in modes and turn == 1:
            entries.append("{this is not json")
            entries.append(json.dumps({**transcript._base("mystery", transcript.last_uuid, 0.1), "message": {"role": "system"}}))
        entries.append(transcript.tool_result(call_id, "total 24\n-rw-r--r-- README.md\n-rw-r--r-- main.py\n"))
        call, call_id = transcript.tool_call("Read", {"file_path": f"{PROJECT_CWD}/main.py"})
        entries.append(call)
        entries.append(transcript.tool_result(call_id, "print('hello')\n"))
        entries.append(
            transcript.assistant_text(
                f"Turn {turn} summary: two files, `main.py` prints a greeting and nothing changed since the last turn. "
                "The README is a stub. Next I would add a test before touching behaviour."
            )
        )
    return entries


def write_line(handle, line: str, mode_split: bool, cadence_s: float) -> None:
    if mode_split and len(line) > 40:
        cut = len(line) // 2
        handle.write(line[:cut])
        handle.flush()
        time.sleep(min(cadence_s, 0.4))
        handle.write(line[cut:] + "\n")
    else:
        handle.write(line + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def cmd_play(args: argparse.Namespace) -> None:
    state = load_state()
    modes = set(args.mode or ["normal"])
    unknown = modes - set(MODES)
    if unknown:
        die(f"unknown mode(s): {', '.join(sorted(unknown))}; choose from {', '.join(MODES)}")
    session_id = args.session_id or str(uuid.uuid4())
    path = Path(state["projects"]) / f"{session_id}.jsonl"
    cadence_s = args.cadence_ms / 1000.0
    transcript = Transcript(session_id, datetime.now(timezone.utc) - timedelta(seconds=5))
    entries = synthetic_turns(transcript, args.turns, modes)
    log(f"playing {len(entries)} entries into {path.name} modes={sorted(modes)} cadence={args.cadence_ms}ms")
    started = time.monotonic()
    with open(path, "a") as handle:
        for index, entry in enumerate(entries):
            line = entry if isinstance(entry, str) else json.dumps(entry, separators=(",", ":"))
            if "delayed-first" in modes and index == 1:
                time.sleep(cadence_s * 5)
            write_line(handle, line, "split-lines" in modes, cadence_s)
            if "burst" in modes and index < len(entries) - 1 and (index % 6) != 5:
                continue
            time.sleep(cadence_s)
    elapsed = time.monotonic() - started
    log(f"played in {elapsed:.1f}s; waiting for the runtime host to know the session")

    def find_session():
        # A locally shipped Claude session keeps its transcript uuid as the
        # Longhouse id; hosted imports carry it as provider_session_id instead.
        listing = http("GET", f"{state['base_url']}/api/agents/sessions?limit=20&include_test=true", token=state["token"])
        for session in listing.get("sessions", []):
            if session_id in (session.get("id"), session.get("provider_session_id")):
                return session
        return None

    session = wait_for("session to be ingested", find_session, timeout_s=60)
    state["provider_session_id"] = session_id
    state["session_id"] = session["id"]
    state["transcript"] = str(path)
    state["played"] = {"turns": args.turns, "modes": sorted(modes), "cadence_ms": args.cadence_ms, "entries": len(entries)}
    save_state(state)
    print(json.dumps({"session_id": session["id"], "provider_session_id": session_id, "title": session.get("title")}))


# --------------------------------------------------------------------------
# sim / verdict
# --------------------------------------------------------------------------


def sim_env(state: dict) -> dict:
    return {**os.environ, "SIM_SERVER_URL": state["base_url"], "SIM_AUTH_TOKEN": state["token"]}


def cmd_sim(args: argparse.Namespace) -> None:
    state = load_state()
    session_id = state.get("session_id")
    if not session_id:
        die("no played session; run `simlab.py play` first")
    script = ROOT / "scripts/ops/sim.sh"
    if args.deploy:
        subprocess.run([str(script), "deploy", session_id], env=sim_env(state), check=True)
    else:
        subprocess.run([str(script), "boot"], env=sim_env(state), check=True)
        subprocess.run([str(script), "launch", session_id], env=sim_env(state), check=True)
    state["sim_launched_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


MARK = re.compile(r"session open stage=(?P<stage>\S+) session=(?P<session>\S+) elapsed_ms=(?P<elapsed>\d+)(?P<rest>.*)")


def cmd_verdict(args: argparse.Namespace) -> None:
    state = load_state()
    session_id = state.get("session_id")
    if not session_id:
        die("no played session; run `simlab.py play` first")
    out = subprocess.run(
        [str(ROOT / "scripts/ops/sim.sh"), "logs", "--since", args.since],
        env=sim_env(state),
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    marks: list[dict] = []
    for line in out.splitlines():
        match = MARK.search(line)
        if not match or match.group("session") != session_id:
            continue
        marks.append({"stage": match.group("stage"), "elapsed_ms": int(match.group("elapsed")), "detail": match.group("rest").strip()})
    stages = [m["stage"] for m in marks]

    def first(stage: str) -> int | None:
        for m in marks:
            if m["stage"] == stage:
                return m["elapsed_ms"]
        return None

    checks = [
        {"id": "session_opened", "status": "pass" if "start" in stages else "fail"},
        {"id": "timeline_rendered", "status": "pass" if "webkit_rendered" in stages else "fail"},
        {"id": "stream_connected", "status": "pass" if "stream_connected" in stages else "fail"},
        {"id": "no_stream_stall", "status": "fail" if "stream_stale" in stages else "pass"},
        {"id": "no_decode_failure", "status": "fail" if "stream_decode_failed" in stages else "pass"},
        {"id": "no_request_failure", "status": "fail" if "request_failed" in stages else "pass"},
    ]
    if args.expect_frames:
        checks.append({"id": "live_frames_received", "status": "pass" if "stream_changed" in stages else "fail"})
    metrics = {
        "first_render_ms": first("webkit_rendered"),
        "stream_connected_ms": first("stream_connected"),
        "renders": stages.count("webkit_rendered"),
        "live_frames": stages.count("stream_changed"),
        "history_fills": stages.count("history_fill"),
        "marks": len(marks),
    }
    status = "pass" if all(c["status"] == "pass" for c in checks) and marks else "fail"
    evidence = [
        {"kind": "mark", "at_ms": m["elapsed_ms"], "stage": m["stage"], "detail": m["detail"][:160]}
        for m in marks
        if m["stage"] in {"stream_stale", "stream_decode_failed", "request_failed", "stream_error"}
    ]
    verdict = {
        "scenario": args.scenario,
        "session_id": session_id,
        "status": status,
        "checks": checks,
        "metrics": metrics,
        "evidence": evidence,
        "artifacts": {"scratch": state.get("scratch"), "transcript": state.get("transcript")},
    }
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "verdict.json").write_text(json.dumps(verdict, indent=2))
    print(json.dumps(verdict, indent=2))
    sys.exit(0 if status == "pass" else 2)


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="start a scratch runtime host and machine agent")
    up.add_argument("--port", type=int, default=0)
    up.set_defaults(func=cmd_up)

    sub.add_parser("down", help="stop the scratch runtime host and machine agent").set_defaults(func=cmd_down)

    play = sub.add_parser("play", help="append a synthetic Claude session at a controlled cadence")
    play.add_argument("--turns", type=int, default=3)
    play.add_argument("--cadence-ms", type=int, default=400)
    play.add_argument("--session-id", default=None)
    play.add_argument("--mode", action="append", choices=sorted(MODES), help="; ".join(f"{k}: {v}" for k, v in MODES.items()))
    play.set_defaults(func=cmd_play)

    sim = sub.add_parser("sim", help="launch the simulator app on the played session")
    sim.add_argument("--deploy", action="store_true", help="build and install first")
    sim.set_defaults(func=cmd_sim)

    verdict = sub.add_parser("verdict", help="verdict envelope from the app's own lifecycle marks")
    verdict.add_argument("--since", default="3m")
    verdict.add_argument("--scenario", default="open-live-session")
    verdict.add_argument("--expect-frames", action="store_true", help="require live stream frames after open")
    verdict.set_defaults(func=cmd_verdict)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
