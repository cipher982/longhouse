#!/usr/bin/env python3
"""simlab: a scripted session through the real pipeline, for the simulator lane.

A scratch HOME holds a Claude transcript root; a real Runtime Host runs on a
scratch SQLite database with auth disabled; a real Machine Agent watches the
root and ships what appears there. This driver plays the provider: it appends
transcript lines at a controlled cadence, with adversarial modes, so the
server projects and the iOS app in the Simulator receives real SSE frames.
Nothing here mocks Longhouse.

    simlab.py up                      start server + engine in a scratch root
    simlab.py play [--turns N] [--cadence-ms MS] [--mode MODE ...] [--append]
                                      append a synthetic Claude session (or more turns)
    simlab.py sim [--deploy]          launch the simulator app on the played session
    simlab.py verdict [--since 3m] [--expect-frames] [--expect-abandoned N]
                                      verdict envelope from the app's log + server state
    simlab.py run [SCENARIO ...]      the golden paths, end to end, with one verdict each
    simlab.py down                    stop server + engine (artifacts stay)

State lives in artifacts/simlab/current/simlab.json; verdicts land beside it.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import tempfile
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
    temporary = tempfile.NamedTemporaryFile(
        mode="w",
        dir=RUN_DIR,
        prefix=".simlab-",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(json.dumps(state, indent=2))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, STATE_FILE)
    finally:
        temporary_path.unlink(missing_ok=True)


def record_timing(target: dict | list, phase: str, started: float, **details: str) -> None:
    elapsed_s = round(time.monotonic() - started, 3)
    timing = {"phase": phase, "elapsed_s": elapsed_s, **details}
    if isinstance(target, dict):
        target.setdefault("timings", []).append(timing)
    else:
        target.append(timing)
    log(f"phase {phase}={elapsed_s:.3f}s")


class AppLogStream:
    def __init__(self, state: dict, udid: str | None = None) -> None:
        environment = sim_env(state)
        if udid:
            environment["SIM_UDID"] = udid
        self.process = subprocess.Popen(
            [str(ROOT / "scripts/ops/sim.sh"), "logs", "--follow"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        if self.process.stdout is None:
            return
        with self.process.stdout:
            for line in self.process.stdout:
                with self._lock:
                    self._lines.append(line)

    def text(self) -> str:
        with self._lock:
            return "".join(self._lines)

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                pass
            self.process.wait()


_app_log_stream: AppLogStream | None = None


def start_app_log_stream(state: dict, udid: str | None = None) -> None:
    global _app_log_stream
    if _app_log_stream is not None and _app_log_stream.process.poll() is None:
        return
    _app_log_stream = AppLogStream(state, udid)


def stop_app_log_stream() -> None:
    if _app_log_stream is not None:
        _app_log_stream.stop()


atexit.register(stop_app_log_stream)


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


def appended_log_contains(path: Path, needle: str):
    offset = 0
    carry = ""

    def predicate():
        nonlocal offset, carry
        if not path.exists():
            return None
        with path.open() as handle:
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
        carry = (carry + chunk)[-len(needle) :]
        return needle in chunk or needle in carry

    return predicate


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
    """The newest built binary across profiles: a stale release build once
    shipped a scenario without the feature under test."""
    candidates: list[Path] = []
    for profile in ("release", "ci"):
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts/build/cargo.py"), "artifact", "--profile", profile, "--bin", name],
            capture_output=True,
            text=True,
            check=False,
        )
        candidate = Path(out.stdout.strip())
        if out.returncode == 0 and candidate.is_file():
            candidates.append(candidate)
    if not candidates:
        die(f"no built {name}; run `simlab.py up --build`")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def build_binaries() -> None:
    log("building longhouse and longhouse-engine (ci profile)")
    subprocess.run([sys.executable, str(ROOT / "scripts/build/generate_build_identity.py")], check=True, capture_output=True)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build/cargo.py"), "exec", "--", "build", "--manifest-path", str(ROOT / "engine/Cargo.toml"),
         "--profile", "ci", "--bin", "longhouse", "--bin", "longhouse-engine"],
        check=True,
    )


def cmd_up(args: argparse.Namespace) -> None:
    up_started = time.monotonic()
    timings: list[dict] = []
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if any(alive(pid) for pid in (state.get("server_pid", 0), state.get("engine_pid", 0))):
            die("a scratch run is still up; `simlab.py down` first")
    if args.build:
        started = time.monotonic()
        build_binaries()
        record_timing(timings, "build", started)
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
    started = time.monotonic()
    wait_for("runtime host health", lambda: http("GET", f"{base_url}/api/health", timeout=2), timeout_s=90)
    record_timing(timings, "server_readiness", started)

    started = time.monotonic()
    token = http("POST", f"{base_url}/api/devices/tokens", {"name": "simlab", "device_id": DEVICE_ID})["token"]
    if not token.startswith("zdt_"):
        die(f"expected a device token, got {token[:8]}")
    record_timing(timings, "token_mint", started)

    agent_env = {**os.environ, "HOME": str(home), "LONGHOUSE_HOME": str(home / ".longhouse"), "RUST_LOG": "info"}
    # `auth` records the machine identity the storage handshake later checks
    # against; the machine name must be the device id the token was minted for.
    started = time.monotonic()
    subprocess.run(
        [str(built_binary("longhouse")), "auth", "--url", base_url],
        env={**agent_env, "LONGHOUSE_DEVICE_TOKEN": token},
        check=True,
        capture_output=True,
        text=True,
    )
    record_timing(timings, "engine_auth", started)
    engine_log = open(scratch / "engine.log", "w")
    started = time.monotonic()
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
        "timings": timings,
    }
    save_state(state)
    wait_for(
        "machine agent control channel",
        appended_log_contains(scratch / "server.log", "control/ws accepted"),
        timeout_s=60,
    )
    record_timing(state, "engine_attach", started)
    record_timing(state, "up", up_started)
    save_state(state)
    log("up")
    print(json.dumps({k: state[k] for k in ("base_url", "token", "scratch")}))


def cmd_down(_: argparse.Namespace) -> None:
    if not STATE_FILE.exists():
        log("nothing to stop")
        return
    state = json.loads(STATE_FILE.read_text())
    started = time.monotonic()
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
    record_timing(state, "down", started)
    save_state(state)
    log(f"down; artifacts in {state.get('scratch')}")


# --------------------------------------------------------------------------
# play
# --------------------------------------------------------------------------


class Transcript:
    """Builds Claude Code JSONL entries with correct parent links."""

    def __init__(self, session_id: str, start: datetime, last_uuid: str | None = None, first_turn: int = 1) -> None:
        self.session_id = session_id
        self.clock = start
        self.last_uuid = last_uuid
        self.first_turn = first_turn

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


ABANDONED_TEXT = "This send was abandoned by an escape."
RESENT_TEXT = "This is the resend after the escape, and it is the one that counts."


def synthetic_turns(transcript: Transcript, turns: int, modes: set[str]) -> list[dict | str]:
    """Entries (or raw lines for malformed mode) for N user turns."""
    entries: list[dict | str] = []
    for offset in range(turns):
        turn = transcript.first_turn + offset
        prompt = f"Turn {turn}: list the files in the project and summarise what changed."
        if "abandon-resend" in modes and offset == turns - 1:
            anchor = transcript.last_uuid
            entries.append(transcript.user(ABANDONED_TEXT, seconds=3.0))
            # Escape-and-resend: a sibling of the first send, same parent.
            entries.append(transcript.user(RESENT_TEXT, parent=anchor, seconds=14.0))
        else:
            entries.append(transcript.user(prompt))
        entries.append(transcript.assistant_text(f"Looking at turn {turn} now. I'll list the tree first."))
        call, call_id = transcript.tool_call("Bash", {"command": "ls -la", "description": "List files"})
        entries.append(call)
        if "malformed" in modes and offset == 0:
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


def transcript_tail(path: Path) -> tuple[str | None, int]:
    """Last chained uuid and number of user turns already in the file."""
    last_uuid = None
    turns = 0
    if not path.exists():
        return None, 0
    for raw in path.read_text().splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or "uuid" not in entry:
            continue
        last_uuid = entry["uuid"]
        message = entry.get("message") or {}
        if entry.get("type") == "user" and isinstance(message.get("content"), str):
            turns += 1
    return last_uuid, turns


def find_session(state: dict, session_id: str) -> dict | None:
    # A locally shipped Claude session keeps its transcript uuid as the
    # Longhouse id; hosted imports carry it as provider_session_id instead.
    listing = http("GET", f"{state['base_url']}/api/agents/sessions?limit=100&include_test=true", token=state["token"])
    for session in listing.get("sessions", []):
        if session_id in (session.get("id"), session.get("provider_session_id")):
            return session
    return None


def play(state: dict, turns: int, cadence_ms: int, modes: set[str], session_id: str | None, append: bool) -> dict:
    unknown = modes - set(MODES)
    if unknown:
        die(f"unknown mode(s): {', '.join(sorted(unknown))}; choose from {', '.join(MODES)}")
    if append and not session_id:
        session_id = state.get("provider_session_id")
        if not session_id:
            die("--append needs a played session")
    session_id = session_id or str(uuid.uuid4())
    path = Path(state["projects"]) / f"{session_id}.jsonl"
    cadence_s = cadence_ms / 1000.0
    last_uuid, prior_turns = transcript_tail(path) if append else (None, 0)
    transcript = Transcript(session_id, datetime.now(timezone.utc) - timedelta(seconds=5), last_uuid, prior_turns + 1)
    entries = synthetic_turns(transcript, turns, modes)
    log(f"playing {len(entries)} entries into {path.name} modes={sorted(modes)} cadence={cadence_ms}ms append={append}")
    play_started = time.monotonic()
    write_started = time.monotonic()
    with open(path, "a") as handle:
        for index, entry in enumerate(entries):
            line = entry if isinstance(entry, str) else json.dumps(entry, separators=(",", ":"))
            if "delayed-first" in modes and index == 1:
                time.sleep(cadence_s * 5)
            write_line(handle, line, "split-lines" in modes, cadence_s)
            if "burst" in modes and index < len(entries) - 1 and (index % 6) != 5:
                continue
            time.sleep(cadence_s)
    record_timing(state, "play_write", write_started, session_id=session_id)
    log(f"played in {time.monotonic() - play_started:.1f}s; waiting for the runtime host to know the session")
    ingest_started = time.monotonic()
    session = wait_for("session to be ingested", lambda: find_session(state, session_id), timeout_s=60)
    record_timing(state, "ingest_wait", ingest_started, session_id=session_id)
    state["provider_session_id"] = session_id
    state["session_id"] = session["id"]
    state["transcript"] = str(path)
    state["played"] = {"turns": prior_turns + turns, "modes": sorted(modes), "cadence_ms": cadence_ms, "entries": len(entries)}
    record_timing(state, "play", play_started, session_id=session_id)
    save_state(state)
    return {"session_id": session["id"], "provider_session_id": session_id, "title": session.get("title"), "entries": len(entries)}


def cmd_play(args: argparse.Namespace) -> None:
    state = load_state()
    print(json.dumps(play(state, args.turns, args.cadence_ms, set(args.mode or ["normal"]), args.session_id, args.append)))


# --------------------------------------------------------------------------
# sim / verdict
# --------------------------------------------------------------------------


def sim_env(state: dict) -> dict:
    return {**os.environ, "SIM_SERVER_URL": state["base_url"], "SIM_AUTH_TOKEN": state["token"]}


def sim(state: dict, deploy: bool) -> None:
    session_id = state.get("session_id")
    if not session_id:
        die("no played session; run `simlab.py play` first")
    script = ROOT / "scripts/ops/sim.sh"
    started = time.monotonic()
    if deploy:
        steps = [("boot", "sim_boot"), ("build", "sim_build"), ("install", "sim_install"), ("launch", "sim_launch")]
    else:
        steps = [("boot", "sim_boot"), ("launch", "sim_launch")]
    for command, phase in steps:
        step_started = time.monotonic()
        command_args = [str(script), command, session_id] if command == "launch" else [str(script), command]
        if command == "boot":
            result = subprocess.run(command_args, env=sim_env(state), capture_output=True, text=True, check=True)
            if result.stdout:
                print(result.stdout, end="", flush=True)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            match = re.search(r"^booted\s+(\S+)", result.stdout, re.MULTILINE)
            if not match:
                die("sim boot did not report a device UDID")
            udid = match.group(1)
        else:
            subprocess.run(command_args, env=sim_env(state), check=True)
        record_timing(state, phase, step_started)
        if command == "boot":
            start_app_log_stream(state, udid)
    record_timing(state, "sim", started)
    state["sim_launched_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def cmd_sim(args: argparse.Namespace) -> None:
    sim(load_state(), args.deploy)


MARK = re.compile(r"session open stage=(?P<stage>\S+) session=(?P<session>\S+) elapsed_ms=(?P<elapsed>\d+)(?P<rest>.*)")


def app_marks(state: dict, session_id: str, since: str) -> list[dict]:
    if _app_log_stream is not None and _app_log_stream.process.poll() is None:
        out = _app_log_stream.text()
    else:
        out = subprocess.run(
            [str(ROOT / "scripts/ops/sim.sh"), "logs", "--since", since],
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
    return marks


def wait_for_app_mark(state: dict, session_id: str, stage: str, count: int, timeout_s: float = 60) -> None:
    wait_for(
        f"{stage} app mark {count}",
        lambda: sum(mark["stage"] == stage for mark in app_marks(state, session_id, "30s")) >= count,
        timeout_s=timeout_s,
        interval_s=0.25,
    )


def server_projection(state: dict, session_id: str) -> dict:
    return http("GET", f"{state['base_url']}/api/agents/sessions/{session_id}/workspace", token=state["token"], timeout=20)


def verdict(state: dict, scenario: str, since: str, expect_frames: bool, expect_abandoned: int | None, expect_user_turns: int | None) -> dict:
    session_id = state.get("session_id")
    if not session_id:
        die("no played session; run `simlab.py play` first")
    verdict_started = time.monotonic()
    marks_started = time.monotonic()
    marks = app_marks(state, session_id, since)
    record_timing(state, "verdict_app_logs", marks_started, scenario=scenario)
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
    if expect_frames:
        checks.append({"id": "live_frames_received", "status": "pass" if "stream_changed" in stages else "fail"})

    # Durable state is the verifier, not the screenshot: the server's own
    # head projection says what the app was given to render.
    projection_started = time.monotonic()
    projection = server_projection(state, session_id)["projection"]
    record_timing(state, "verdict_projection", projection_started, scenario=scenario)
    user_items = [
        item for item in projection.get("items", [])
        if (item.get("event") or {}).get("role") == "user" and isinstance((item.get("event") or {}).get("content_text"), str)
        and not (item.get("event") or {}).get("tool_name")
    ]
    user_texts = [item["event"]["content_text"] for item in user_items]
    if expect_abandoned is not None:
        checks.append({"id": "abandoned_counted", "status": "pass" if projection.get("abandoned_events") == expect_abandoned else "fail",
                       "detail": f"abandoned_events={projection.get('abandoned_events')}"})
        checks.append({"id": "abandoned_hidden_from_head", "status": "fail" if ABANDONED_TEXT in user_texts else "pass"})
        checks.append({"id": "resend_on_head", "status": "pass" if RESENT_TEXT in user_texts else "fail"})
    if expect_user_turns is not None:
        checks.append({"id": "user_turns_projected", "status": "pass" if len(user_texts) == expect_user_turns else "fail",
                       "detail": f"user_items={len(user_texts)}"})

    metrics = {
        "first_render_ms": first("webkit_rendered"),
        "stream_connected_ms": first("stream_connected"),
        "renders": stages.count("webkit_rendered"),
        "live_frames": stages.count("stream_changed"),
        "history_fills": stages.count("history_fill"),
        "marks": len(marks),
        "projected_items": len(projection.get("items", [])),
        "abandoned_events": projection.get("abandoned_events"),
    }
    status = "pass" if all(c["status"] == "pass" for c in checks) and marks else "fail"
    evidence = [
        {"kind": "mark", "at_ms": m["elapsed_ms"], "stage": m["stage"], "detail": m["detail"][:160]}
        for m in marks
        if m["stage"] in {"stream_stale", "stream_decode_failed", "request_failed", "stream_error"}
    ]
    envelope = {
        "scenario": scenario,
        "session_id": session_id,
        "status": status,
        "checks": checks,
        "metrics": metrics,
        "evidence": evidence,
        "artifacts": {"scratch": state.get("scratch"), "transcript": state.get("transcript")},
    }
    record_timing(state, "verdict", verdict_started, scenario=scenario)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / f"verdict-{scenario}.json").write_text(json.dumps(envelope, indent=2))
    save_state(state)
    return envelope


def cmd_verdict(args: argparse.Namespace) -> None:
    envelope = verdict(load_state(), args.scenario, args.since, args.expect_frames, args.expect_abandoned, args.expect_user_turns)
    print(json.dumps(envelope, indent=2))
    sys.exit(0 if envelope["status"] == "pass" else 2)


# --------------------------------------------------------------------------
# run: the golden paths
# --------------------------------------------------------------------------


def settle(state: dict, session_id: str, stage: str, count: int, phase: str) -> None:
    started = time.monotonic()
    wait_for_app_mark(state, session_id, stage, count)
    record_timing(state, phase, started)
    save_state(state)


def scenario_open_imported(state: dict, deploy: bool) -> dict:
    play(state, turns=2, cadence_ms=150, modes={"normal"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, state["session_id"], "webkit_rendered", 1, "settle_open_imported")
    return verdict(state, "open-imported-session", "2m", False, None, 2)


def scenario_live_turns(state: dict, deploy: bool) -> dict:
    play(state, turns=1, cadence_ms=150, modes={"normal"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, state["session_id"], "webkit_rendered", 1, "settle_live_open")
    play(state, turns=2, cadence_ms=300, modes={"normal"}, session_id=None, append=True)
    settle(state, state["session_id"], "webkit_rendered", 2, "settle_live_turns")
    return verdict(state, "live-turns-into-open-session", "3m", True, None, 3)


def scenario_escape_resend(state: dict, deploy: bool) -> dict:
    play(state, turns=2, cadence_ms=150, modes={"abandon-resend"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, state["session_id"], "webkit_rendered", 1, "settle_escape_resend")
    return verdict(state, "escape-and-resend", "2m", False, 1, 2)


def scenario_hostile_transcript(state: dict, deploy: bool) -> dict:
    play(state, turns=2, cadence_ms=150, modes={"malformed", "split-lines", "delayed-first"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, state["session_id"], "webkit_rendered", 1, "settle_hostile")
    return verdict(state, "hostile-transcript", "2m", False, None, 2)


SCENARIOS = {
    "open-imported-session": scenario_open_imported,
    "live-turns-into-open-session": scenario_live_turns,
    "escape-and-resend": scenario_escape_resend,
    "hostile-transcript": scenario_hostile_transcript,
}


def cmd_run(args: argparse.Namespace) -> None:
    state = load_state()
    names = args.scenario or list(SCENARIOS)
    unknown = set(names) - set(SCENARIOS)
    if unknown:
        die(f"unknown scenario(s): {', '.join(sorted(unknown))}; choose from {', '.join(SCENARIOS)}")
    results = []
    deploy = args.deploy
    for name in names:
        log(f"=== scenario {name}")
        envelope = SCENARIOS[name](state, deploy)
        deploy = False
        results.append(envelope)
        failed = [c["id"] for c in envelope["checks"] if c["status"] != "pass"]
        log(f"=== {name}: {envelope['status']}" + (f" ({', '.join(failed)})" if failed else ""))
    summary = {
        "status": "pass" if all(r["status"] == "pass" for r in results) else "fail",
        "scenarios": [
            {
                "scenario": r["scenario"],
                "status": r["status"],
                "failed_checks": [c["id"] for c in r["checks"] if c["status"] != "pass"],
                "stream_connected_ms": r["metrics"].get("stream_connected_ms"),
                "first_render_ms": r["metrics"].get("first_render_ms"),
                "live_frames": r["metrics"].get("live_frames"),
            }
            for r in results
        ],
    }
    (RUN_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["status"] == "pass" else 2)


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="start a scratch runtime host and machine agent")
    up.add_argument("--port", type=int, default=0)
    up.add_argument("--build", action="store_true", help="build the engine binaries from this checkout first")
    up.set_defaults(func=cmd_up)

    sub.add_parser("down", help="stop the scratch runtime host and machine agent").set_defaults(func=cmd_down)

    play_parser = sub.add_parser("play", help="append a synthetic Claude session at a controlled cadence")
    play_parser.add_argument("--turns", type=int, default=3)
    play_parser.add_argument("--cadence-ms", type=int, default=400)
    play_parser.add_argument("--session-id", default=None)
    play_parser.add_argument("--append", action="store_true", help="add turns to the played session instead of a new one")
    play_parser.add_argument("--mode", action="append", choices=sorted(MODES), help="; ".join(f"{k}: {v}" for k, v in MODES.items()))
    play_parser.set_defaults(func=cmd_play)

    sim_parser = sub.add_parser("sim", help="launch the simulator app on the played session")
    sim_parser.add_argument("--deploy", action="store_true", help="build and install first")
    sim_parser.set_defaults(func=cmd_sim)

    verdict_parser = sub.add_parser("verdict", help="verdict envelope from the app's own lifecycle marks and server state")
    verdict_parser.add_argument("--since", default="3m")
    verdict_parser.add_argument("--scenario", default="ad-hoc")
    verdict_parser.add_argument("--expect-frames", action="store_true", help="require live stream frames after open")
    verdict_parser.add_argument("--expect-abandoned", type=int, default=None, help="require this many abandoned events in the head projection")
    verdict_parser.add_argument("--expect-user-turns", type=int, default=None, help="require this many user messages on the head projection")
    verdict_parser.set_defaults(func=cmd_verdict)

    run_parser = sub.add_parser("run", help="run golden-path scenarios end to end")
    run_parser.add_argument("scenario", nargs="*", choices=sorted(SCENARIOS) + [], help="default: all")
    run_parser.add_argument("--deploy", action="store_true", help="build and install the app before the first scenario")
    run_parser.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
