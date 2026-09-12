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
import base64
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
from subprocess import Popen

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "artifacts" / "simlab" / "current"
STATE_FILE = RUN_DIR / "simlab.json"
SCRATCH_ROOT = Path("/tmp/longhouse-simlab")
# Shadow imports have no launch-registration API. Use the existing hidden
# provider-evidence namespace; never turn them into Console sessions for QA.
PROJECT_CWD = "/tmp/longhouse-simlab/evidence/raw/project"
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
    raise RuntimeError(msg)


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
        self.process = Popen(
            [str(ROOT / "scripts/ops/sim.sh"), "logs", "--follow"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.log_path = Path(state["scratch"]) / "app.log"
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        if self.process.stdout is None:
            return
        with self.process.stdout, self.log_path.open("a", buffering=1) as output:
            for line in self.process.stdout:
                output.write(line)
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
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # noqa: BLE001 - readiness probes fail until they succeed
            last_error = f"{type(exc).__name__}: {exc}"
            result = None
        if result:
            return result
        time.sleep(interval_s)
    detail = f"; last probe error: {last_error}" if last_error else ""
    raise TimeoutError(f"timed out waiting for {description}{detail}")


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


def process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    # argv can change after exec (notably Python launchers); process birth
    # time stays stable and distinguishes a reused PID from this run's child.
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True, text=True, check=False, timeout=5,
        env={**os.environ, "LC_ALL": "C"},
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def owns_process(state: dict, key: str) -> bool:
    expected = state.get("process_identities", {}).get(key)
    pid = state.get(key, 0)
    if not expected or process_identity(pid) != expected:
        return False
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


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
    if STATE_FILE.exists():
        previous = load_state()
        if any(owns_process(previous, key) for key in ("server_pid", "engine_pid", "proxy_pid")):
            die("a scratch run is still up; `simlab.py down` first")
    up_started = time.monotonic()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # Short enough for the engine's Unix sockets under macOS's 104-byte cap.
    scratch = SCRATCH_ROOT / stamp
    home = scratch / "home"
    projects = home / ".claude" / "projects" / PROJECT_CWD.replace("/", "-")
    projects.mkdir(parents=True)
    (home / ".longhouse").mkdir()
    port = args.port or free_port()
    base_url = f"http://127.0.0.1:{port}"
    state = {
        "scratch": str(scratch),
        "home": str(home),
        "projects": str(projects),
        "base_url": base_url,
        "started_at": stamp,
        "startup_status": "starting",
        "timings": [],
    }
    save_state(state)
    processes: list[Popen] = []

    def start(key: str, command: list[str], environment: dict, log_name: str, cwd: Path | None = None):
        with (scratch / log_name).open("w") as output:
            process = Popen(
                command, cwd=cwd, env=environment, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        processes.append(process)
        state[key] = process.pid
        identity = process_identity(process.pid)
        if not identity:
            die(f"{key} exited before its process identity could be recorded")
        state.setdefault("process_identities", {})[key] = identity
        save_state(state)
        return process

    try:
        if args.build:
            started = time.monotonic()
            build_binaries()
            record_timing(state, "build", started)
        fernet = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        server = start(
            "server_pid",
            ["uv", "run", "python", "-m", "zerg.cli.main", "serve", "--host", "127.0.0.1", "--port", str(port)],
            {
                **os.environ,
                "AUTH_DISABLED": "1",
                "LLM_DISABLED": "1",
                "LOG_LEVEL": "INFO",
                "DATABASE_URL": f"sqlite:///{scratch / 'longhouse.db'}",
                "JWT_SECRET": "simlab-jwt-secret",
                "FERNET_SECRET": fernet,
                "INTERNAL_API_SECRET": "simlab-internal-secret",
            },
            "server.log", ROOT / "server",
        )
        log(f"runtime host starting on {base_url} (pid {server.pid})")
        started = time.monotonic()
        wait_for("runtime host health", lambda: http("GET", f"{base_url}/api/health", timeout=2), timeout_s=90)
        record_timing(state, "server_readiness", started)
        started = time.monotonic()
        token = http("POST", f"{base_url}/api/devices/tokens", {"name": "simlab", "device_id": DEVICE_ID})["token"]
        if not token.startswith("zdt_"):
            die("expected a device token from the scratch runtime")
        state["token"] = token
        record_timing(state, "token_mint", started)

        proxy_port = free_port()
        state["client_url"] = f"http://127.0.0.1:{proxy_port}"
        state["network_offline_file"] = str(scratch / "client-offline")
        start(
            "proxy_pid",
            [sys.executable, str(ROOT / "scripts/qa/simlab_proxy.py"),
             "--listen-port", str(proxy_port), "--upstream-port", str(port),
             "--offline-file", state["network_offline_file"]],
            dict(os.environ), "proxy.log",
        )
        started = time.monotonic()
        wait_for("client relay health", lambda: http("GET", f"{state['client_url']}/api/health", timeout=2), timeout_s=15)
        record_timing(state, "proxy_readiness", started)

        # Explicit provider roots prevent inherited overrides reaching real user data.
        agent_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "HOME": str(home),
            "LONGHOUSE_HOME": str(home / ".longhouse"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "RUST_LOG": "info",
        }
        started = time.monotonic()
        subprocess.run(
            [str(built_binary("longhouse")), "auth", "--url", base_url, "--device", DEVICE_ID],
            env={**agent_env, "LONGHOUSE_DEVICE_TOKEN": token},
            check=True, capture_output=True, text=True,
        )
        record_timing(state, "engine_auth", started)
        started = time.monotonic()
        engine = start(
            "engine_pid",
            [str(built_binary("longhouse-engine")), "connect", "--url", base_url,
             "--token", token, "--db", str(scratch / "agent.db"),
             "--machine-name", DEVICE_ID, "--fallback-scan-secs", "2", "--spool-replay-secs", "1"],
            agent_env, "engine.log",
        )
        log(f"machine agent starting (pid {engine.pid}), watching {projects}")
        wait_for(
            "machine agent control channel",
            appended_log_contains(scratch / "server.log", "control/ws accepted"), timeout_s=60,
        )
        record_timing(state, "engine_attach", started)
        record_timing(state, "up", up_started)
        state["startup_status"] = "ready"
        save_state(state)
    except (Exception, KeyboardInterrupt) as exc:
        cleanup_errors = []
        for process in reversed(processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
        for key in ("engine_pid", "proxy_pid", "server_pid"):
            process = next((item for item in processes if item.pid == state.get(key)), None)
            if process is not None and process.poll() is not None:
                state.pop(key, None)
        state["startup_status"] = "failed"
        state["startup_error"] = f"{type(exc).__name__}: {exc}"
        state["cleanup_errors"] = cleanup_errors
        save_state(state)
        envelope = failure_verdict(state, "startup", exc, capture_client=False)
        write_summary([envelope])
        raise
    log("up")
    print(json.dumps({k: state[k] for k in ("base_url", "client_url", "token", "scratch")}))


def cmd_down(_: argparse.Namespace) -> None:
    if not STATE_FILE.exists():
        log("nothing to stop")
        return
    state = json.loads(STATE_FILE.read_text())
    started = time.monotonic()
    if state.get("sim_udid"):
        # Stop only the app on the simulator selected by this scratch run.
        # Leaving it alive would keep polling a Runtime Host we are removing.
        try:
            subprocess.run(
                ["xcrun", "simctl", "terminate", state["sim_udid"], "ai.longhouse.ios"],
                check=False, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"simulator app cleanup failed: {exc}; stopping scratch services anyway")
    for key in ("engine_pid", "proxy_pid", "server_pid"):
        pid = state.get(key, 0)
        if owns_process(state, key):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    time.sleep(1)
    for key in ("engine_pid", "proxy_pid", "server_pid"):
        pid = state.get(key, 0)
        if owns_process(state, key):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                log(f"could not stop {key}={pid}: {exc}")
                continue
        state.pop(key, None)
        state.get("process_identities", {}).pop(key, None)
    state.pop("sim_udid", None)
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
    # Locally shipped Claude IDs are canonical. Direct reads can open hidden
    # provider-proof sessions without weakening the default timeline filter.
    try:
        return http("GET", f"{state['base_url']}/api/agents/sessions/{session_id}", token=state["token"])
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


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
    state["provider_session_id"] = session_id
    state["session_id"] = session_id
    state["transcript"] = str(path)
    if entries:
        state["expected_assistant_text"] = entries[-1]["message"]["content"][0]["text"]
        state["expected_assistant_uuid"] = entries[-1]["uuid"]
    save_state(state)
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
    environment = {**os.environ, "SIM_SERVER_URL": state.get("client_url", state["base_url"]), "SIM_AUTH_TOKEN": state["token"]}
    if state.get("sim_udid"):
        environment["SIM_UDID"] = state["sim_udid"]
    return environment


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
            state["sim_udid"] = udid
            save_state(state)
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


def app_log_text(state: dict, since: str) -> str:
    if _app_log_stream is not None:
        if _app_log_stream.process.poll() is not None:
            die("app log follower exited; recovery boundaries cannot switch log sources")
        return _app_log_stream.text()
    captured = Path(state["scratch"]) / "app.log" if state.get("scratch") else None
    if captured is not None and captured.exists():
        return captured.read_text()
    if state.get("render_expectation"):
        die("recorded app log missing; recovery boundaries cannot use a sliding log window")
    result = subprocess.run(
        [str(ROOT / "scripts/ops/sim.sh"), "logs", "--since", since],
        env=sim_env(state), capture_output=True, text=True, check=True, timeout=30,
    )
    return result.stdout


def parse_app_marks(text: str, session_id: str) -> list[dict]:
    marks = []
    for line in text.splitlines():
        match = MARK.search(line)
        if match and match.group("session") == session_id:
            detail = match.group("rest").strip()
            marks.append({
                "stage": match.group("stage"), "elapsed_ms": int(match.group("elapsed")),
                "detail": detail, "fields": dict(re.findall(r"(\w+)=(\S+)", detail)),
            })
    return marks


def app_marks(state: dict, session_id: str, since: str) -> list[dict]:
    return parse_app_marks(app_log_text(state, since), session_id)


def latest_open_marks(marks: list[dict], minimum_starts: int = 1) -> list[dict]:
    starts = [index for index, mark in enumerate(marks) if mark["stage"] == "start"]
    return marks[starts[-1]:] if len(starts) >= minimum_starts else []


def rendered_latest(marks: list[dict], expected: str | None, minimum_starts: int = 1) -> dict | None:
    if not expected:
        return None
    for mark in reversed(latest_open_marks(marks, minimum_starts)):
        if mark["stage"] != "webkit_rendered":
            continue
        fields = mark.get("fields", {})
        try:
            if (
                fields.get("latest") == expected
                and int(fields.get("rows", "0")) > 0
                and int(fields.get("sequence", "0")) > 0
                and int(fields.get("js_failures", "-1")) == 0
            ):
                return mark
        except ValueError:
            pass
        return None
    return None


def projected_user_texts(projection: dict) -> list[str]:
    return [
        item["event"]["content_text"] for item in projection.get("items", [])
        if (item.get("event") or {}).get("role") == "user"
        and isinstance((item.get("event") or {}).get("content_text"), str)
        and not (item.get("event") or {}).get("tool_name")
    ]


def projected_latest(projection: dict) -> str | None:
    # WebTranscriptPayload uses TimelineItem.id, not SessionProjectionItem.id.
    # These scenarios finish with prose, whose rendered key is prose:<event.id>.
    items = projection.get("items", [])
    if not items or items[-1].get("kind") != "event":
        return None
    event = items[-1].get("event") or {}
    if event.get("role") != "assistant" or not event.get("content_text") or event.get("tool_name"):
        return None
    return f"prose:{event['id']}" if event.get("id") is not None else None


def projection_complete(workspace: dict, turns: int, expected_text: str | None) -> bool:
    projection = workspace.get("projection", {})
    if not expected_text or len(projected_user_texts(projection)) != turns or not projected_latest(projection):
        return False
    # Match the actual final assistant entry we wrote, not an incomplete head
    # that already has all user prompts but is still missing its last reply.
    return projection["items"][-1]["event"]["content_text"] == expected_text


def server_projection(state: dict, session_id: str) -> dict:
    return http("GET", f"{state['base_url']}/api/agents/sessions/{session_id}/workspace", token=state["token"], timeout=20)


def evaluate_verdict(
    marks: list[dict], workspace: dict, *, expect_frames: bool,
    expect_abandoned: int | None, expect_user_turns: int | None,
    expected_latest: str | None = None, minimum_starts: int = 1,
    allow_transport_fault: bool = False, expected_text: str | None = None,
    network_boundary: dict | None = None,
) -> dict:
    projection = workspace.get("projection", {})
    active_marks = latest_open_marks(marks, minimum_starts)
    stages = [mark["stage"] for mark in active_marks]
    all_stages = [mark["stage"] for mark in marks]
    expected_latest = expected_latest or projected_latest(projection)
    rendered = rendered_latest(marks, expected_latest, minimum_starts)
    checks = []

    def check(identifier: str, passed: bool, detail: str = "") -> None:
        checks.append({"id": identifier, "status": "pass" if passed else "fail", "detail": detail})

    check("session_opened", "start" in stages, f"required_starts={minimum_starts} observed={all_stages.count('start')}")
    check("timeline_rendered", "webkit_rendered" in stages)
    check("client_latest_rendered", rendered is not None, f"expected_latest={expected_latest}")
    check("client_matches_projection", expected_latest is not None and expected_latest == projected_latest(projection))
    check("test_session_hidden", workspace.get("session", {}).get("hidden_from_default_timeline") is True)
    check("stream_connected", "stream_connected" in stages)
    check("no_decode_failure", "stream_decode_failed" not in all_stages)
    check("no_webkit_failure", "webkit_failed" not in all_stages and all(
        mark.get("fields", {}).get("js_failures", "0") == "0"
        for mark in marks if mark["stage"] == "webkit_rendered"
    ))
    if not allow_transport_fault:
        check("no_stream_stall", "stream_stale" not in all_stages)
        check("no_request_failure", "request_failed" not in all_stages)
        check("no_stream_error", "stream_error" not in all_stages)
    if network_boundary is not None:
        initial_end = network_boundary.get("initial_marks", 0)
        offline_end = network_boundary.get("offline_marks", 0)
        valid_boundary = 0 < initial_end < offline_end <= len(marks)
        check("client_stream_disconnected", valid_boundary and any(
            mark["stage"] == "stream_disconnected" for mark in marks[initial_end:offline_end]
        ))
        check("client_stream_reconnected", valid_boundary and any(
            mark["stage"] == "stream_connected" for mark in marks[offline_end:]
        ))
        check("network_recovery_without_relaunch", valid_boundary and (
            sum(mark["stage"] == "start" for mark in marks[:initial_end]) == all_stages.count("start")
        ))
    if expect_frames:
        check("live_frames_received", "stream_changed" in stages)
    user_texts = projected_user_texts(projection)
    if expect_abandoned is not None:
        check("abandoned_counted", projection.get("abandoned_events") == expect_abandoned,
              f"abandoned_events={projection.get('abandoned_events')}")
        check("abandoned_hidden_from_head", ABANDONED_TEXT not in user_texts)
        check("resend_on_head", RESENT_TEXT in user_texts)
    if expect_user_turns is not None:
        check("user_turns_projected", len(user_texts) == expect_user_turns, f"user_items={len(user_texts)}")
        check("source_tail_projected", projection_complete(workspace, expect_user_turns, expected_text))

    def first(stage: str) -> int | None:
        return next((mark["elapsed_ms"] for mark in active_marks if mark["stage"] == stage), None)

    return {
        "status": "pass" if all(check["status"] == "pass" for check in checks) else "fail",
        "checks": checks,
        "metrics": {
            "first_render_ms": first("webkit_rendered"), "stream_connected_ms": first("stream_connected"),
            "renders": stages.count("webkit_rendered"), "live_frames": stages.count("stream_changed"),
            "history_fills": stages.count("history_fill"), "marks": len(marks),
            "projected_items": len(projection.get("items", [])), "abandoned_events": projection.get("abandoned_events"),
        },
        "evidence": {
            "expected_latest": expected_latest, "minimum_starts": minimum_starts,
            "expected_assistant_text": expected_text,
            "matching_client_render": rendered, "transport_fault_expected": allow_transport_fault,
        },
    }


def scenario_artifact_dir(state: dict, scenario: str) -> Path:
    root = Path(state["scratch"]) / "artifacts" if state.get("scratch") else RUN_DIR
    path = root / re.sub(r"[^a-zA-Z0-9_-]", "_", scenario)
    path.mkdir(parents=True, exist_ok=True)
    return path


def screenshot(state: dict, scenario: str, label: str) -> str:
    if not state.get("sim_udid"):
        die("no simulator UDID recorded; cannot capture client evidence")
    path = scenario_artifact_dir(state, scenario) / f"{label}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", state["sim_udid"], "screenshot", str(path)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    state.setdefault("screenshots", {})[label] = str(path)
    save_state(state)
    return str(path)


def collect_evidence(state: dict, scenario: str, since: str, capture_client: bool = True) -> tuple[dict, list[dict], dict, list[str]]:
    directory = scenario_artifact_dir(state, scenario)
    artifacts = {
        "scratch": state.get("scratch"), "transcript": state.get("transcript"),
        "screenshots": dict(state.get("screenshots", {})),
        "app_log_source": "follower" if _app_log_stream is not None else "persisted_or_ad_hoc",
    }
    if state.get("scratch"):
        artifacts["service_logs"] = {name: str(Path(state["scratch"]) / f"{name}.log") for name in ("server", "engine", "proxy")}
        artifacts["run_app_log"] = str(Path(state["scratch"]) / "app.log")
    errors = []
    marks = []
    workspace = {}
    if capture_client:
        try:
            artifacts["screenshots"]["final"] = screenshot(state, scenario, "final")
        except Exception as exc:
            errors.append(f"screenshot: {type(exc).__name__}: {exc}")
        try:
            text = app_log_text(state, since)
            (directory / "app.log").write_text(text)
            artifacts["app_log"] = str(directory / "app.log")
            marks = parse_app_marks(text, state.get("session_id", ""))
        except Exception as exc:
            errors.append(f"app diagnostics: {type(exc).__name__}: {exc}")
    (directory / "diagnostics.json").write_text(json.dumps(marks, indent=2))
    artifacts["diagnostics"] = str(directory / "diagnostics.json")
    if state.get("session_id"):
        try:
            workspace = server_projection(state, state["session_id"])
        except Exception as exc:
            errors.append(f"server projection: {type(exc).__name__}: {exc}")
    (directory / "workspace.json").write_text(json.dumps(workspace, indent=2))
    artifacts["workspace"] = str(directory / "workspace.json")
    if state.get("recovery"):
        (directory / "recovery.json").write_text(json.dumps(state["recovery"], indent=2))
        artifacts["recovery"] = str(directory / "recovery.json")
    artifacts["capture_errors"] = errors
    return artifacts, marks, workspace, errors


def save_verdict(state: dict, scenario: str, envelope: dict) -> dict:
    envelope.update({"scenario": scenario, "session_id": state.get("session_id")})
    path = scenario_artifact_dir(state, scenario) / "verdict.json"
    envelope["artifacts"]["verdict"] = str(path)
    path.write_text(json.dumps(envelope, indent=2))
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / f"verdict-{re.sub(r'[^a-zA-Z0-9_-]', '_', scenario)}.json").write_text(json.dumps(envelope, indent=2))
    save_state(state)
    return envelope


def failure_verdict(state: dict, scenario: str, error: BaseException, *, capture_client: bool = True) -> dict:
    artifacts, marks, workspace, _ = collect_evidence(state, scenario, "10m", capture_client)
    detail = f"{type(error).__name__}: {error}"
    if isinstance(error, subprocess.CalledProcessError):
        detail += f"\nstdout: {error.stdout or ''}\nstderr: {error.stderr or ''}"
    return save_verdict(state, scenario, {
        "status": "fail",
        "checks": [{"id": "scenario_completed", "status": "fail", "detail": detail}],
        "metrics": {"marks": len(marks), "projected_items": len(workspace.get("projection", {}).get("items", []))},
        "evidence": {
            "error": detail, "phase": state.get("phase"),
            "expected_assistant_text": state.get("expected_assistant_text"),
            "expected_assistant_uuid": state.get("expected_assistant_uuid"),
            "cleanup_errors": state.get("cleanup_errors", []),
        },
        "artifacts": artifacts,
    })


def verdict(state: dict, scenario: str, since: str, expect_frames: bool, expect_abandoned: int | None, expect_user_turns: int | None) -> dict:
    artifacts, marks, workspace, errors = collect_evidence(state, scenario, since)
    expectation = state.get("render_expectation", {})
    envelope = evaluate_verdict(
        marks, workspace, expect_frames=expect_frames, expect_abandoned=expect_abandoned,
        expect_user_turns=expect_user_turns, expected_latest=expectation.get("latest"),
        minimum_starts=expectation.get("minimum_starts", 1),
        allow_transport_fault=scenario == "client-network-recovery",
        expected_text=state.get("expected_assistant_text"),
        network_boundary=state.get("network_boundary", {}) if scenario == "client-network-recovery" else None,
    )
    envelope["artifacts"] = artifacts
    if errors:
        envelope["status"] = "fail"
        envelope["checks"].append({"id": "evidence_captured", "status": "fail", "detail": "; ".join(errors)})
    return save_verdict(state, scenario, envelope)


def cmd_verdict(args: argparse.Namespace) -> None:
    envelope = verdict(load_state(), args.scenario, args.since, args.expect_frames, args.expect_abandoned, args.expect_user_turns)
    print(json.dumps(envelope, indent=2))
    sys.exit(0 if envelope["status"] == "pass" else 2)


# --------------------------------------------------------------------------
# run: the golden paths
# --------------------------------------------------------------------------


def settle_projection(state: dict, turns: int) -> dict:
    state["phase"] = f"await_projection_{turns}_turns"
    save_state(state)

    def ready():
        workspace = server_projection(state, state["session_id"])
        return workspace if projection_complete(workspace, turns, state.get("expected_assistant_text")) else None

    return wait_for(f"{turns} complete projected turns", ready, timeout_s=60)


def settle(state: dict, turns: int, phase: str, minimum_starts: int = 1) -> dict:
    started = time.monotonic()
    workspace = settle_projection(state, turns)
    expected = projected_latest(workspace["projection"])
    state["render_expectation"] = {"latest": expected, "minimum_starts": minimum_starts}
    state["phase"] = phase
    save_state(state)

    def ready():
        marks = app_marks(state, state["session_id"], "10m")
        active = latest_open_marks(marks, minimum_starts)
        if not any(mark["stage"] == "stream_connected" for mark in active):
            return None
        boundary = state.get("network_boundary")
        if boundary and not any(mark["stage"] == "stream_connected" for mark in marks[boundary["offline_marks"]:]):
            return None
        return rendered_latest(marks, expected, minimum_starts)

    rendered = wait_for(f"client rendered {expected} after {minimum_starts} session open(s)", ready, timeout_s=75)
    record_timing(state, phase, started)
    save_state(state)
    return {"workspace": workspace, "render": rendered, "marks": app_marks(state, state["session_id"], "10m")}


def scenario_open_imported(state: dict, deploy: bool) -> dict:
    play(state, turns=2, cadence_ms=150, modes={"normal"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, 2, "settle_open_imported")
    return verdict(state, "open-imported-session", "2m", False, None, 2)


def scenario_live_turns(state: dict, deploy: bool) -> dict:
    play(state, turns=1, cadence_ms=150, modes={"normal"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, 1, "settle_live_open")
    play(state, turns=2, cadence_ms=300, modes={"normal"}, session_id=None, append=True)
    settle(state, 3, "settle_live_turns")
    return verdict(state, "live-turns-into-open-session", "3m", True, None, 3)


def scenario_escape_resend(state: dict, deploy: bool) -> dict:
    play(state, turns=2, cadence_ms=150, modes={"abandon-resend"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, 2, "settle_escape_resend")
    return verdict(state, "escape-and-resend", "2m", False, 1, 2)


def scenario_hostile_transcript(state: dict, deploy: bool) -> dict:
    play(state, turns=2, cadence_ms=150, modes={"malformed", "split-lines", "delayed-first"}, session_id=None, append=False)
    sim(state, deploy)
    settle(state, 2, "settle_hostile")
    return verdict(state, "hostile-transcript", "2m", False, None, 2)


def scenario_interrupted_client(state: dict, deploy: bool) -> dict:
    scenario = "interrupted-client-recovery"
    play(state, turns=1, cadence_ms=150, modes={"normal"}, session_id=None, append=False)
    sim(state, deploy)
    initial = settle(state, 1, "settle_before_termination")
    state["recovery"] = {"before": initial}
    screenshot(state, scenario, "before-termination")
    minimum_starts = sum(mark["stage"] == "start" for mark in initial["marks"]) + 1
    state["phase"] = "terminate_client"
    save_state(state)
    subprocess.run(
        ["xcrun", "simctl", "terminate", state["sim_udid"], "ai.longhouse.ios"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    state["recovery"]["terminated_at"] = datetime.now(timezone.utc).isoformat()
    screenshot(state, scenario, "client-absent")
    play(state, turns=2, cadence_ms=150, modes={"normal"}, session_id=None, append=True)
    state["recovery"]["while_absent"] = settle_projection(state, 3)
    state["render_expectation"] = {
        "latest": projected_latest(state["recovery"]["while_absent"]["projection"]),
        "minimum_starts": minimum_starts,
    }
    state["phase"] = "relaunch_client"
    save_state(state)
    sim(state, False)
    state["recovery"]["after"] = settle(state, 3, "settle_after_relaunch", minimum_starts)
    save_state(state)
    return verdict(state, scenario, "10m", False, None, 3)


def scenario_network_recovery(state: dict, deploy: bool) -> dict:
    scenario = "client-network-recovery"
    gate = Path(state["network_offline_file"])
    play(state, turns=1, cadence_ms=150, modes={"normal"}, session_id=None, append=False)
    sim(state, deploy)
    initial = settle(state, 1, "settle_before_network_loss")
    state["recovery"] = {"before": initial}
    screenshot(state, scenario, "before-disconnect")
    starts = sum(mark["stage"] == "start" for mark in initial["marks"])
    try:
        gate.touch()
        state["phase"] = "client_network_offline"
        save_state(state)

        def disconnected():
            try:
                http("GET", f"{state['client_url']}/api/health", timeout=2)
            except (OSError, urllib.error.URLError):
                return True
            return False

        wait_for("client relay disconnection", disconnected, timeout_s=10)
        state["recovery"]["disconnected_at"] = datetime.now(timezone.utc).isoformat()

        def client_disconnected():
            marks = app_marks(state, state["session_id"], "10m")
            return marks if any(mark["stage"] == "stream_disconnected" for mark in marks[len(initial["marks"]):]) else None

        wait_for("client stream disconnection during outage", client_disconnected, timeout_s=30)
        play(state, turns=2, cadence_ms=150, modes={"normal"}, session_id=None, append=True)
        state["recovery"]["while_offline"] = settle_projection(state, 3)
        state["recovery"]["offline_marks"] = app_marks(state, state["session_id"], "10m")
        state["network_boundary"] = {
            "initial_marks": len(initial["marks"]),
            "offline_marks": len(state["recovery"]["offline_marks"]),
        }
        screenshot(state, scenario, "network-offline")
        offline_latest = projected_latest(state["recovery"]["while_offline"]["projection"])
        if rendered_latest(state["recovery"]["offline_marks"], offline_latest, starts):
            die("client rendered appended content while relay was offline; disconnection was not proven")
    finally:
        gate.unlink(missing_ok=True)
        state.setdefault("recovery", {})["network_restored_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    recovered = settle(state, 3, "settle_after_network_restore", starts)
    state["recovery"]["after"] = recovered
    save_state(state)
    if sum(mark["stage"] == "start" for mark in recovered["marks"]) != starts:
        die("client relaunched during network-only recovery; reconnect was not proven")
    return verdict(state, scenario, "10m", False, None, 3)


SCENARIOS = {
    "open-imported-session": scenario_open_imported,
    "live-turns-into-open-session": scenario_live_turns,
    "escape-and-resend": scenario_escape_resend,
    "hostile-transcript": scenario_hostile_transcript,
    "interrupted-client-recovery": scenario_interrupted_client,
    "client-network-recovery": scenario_network_recovery,
}


def write_summary(results: list[dict]) -> dict:
    summary = {
        "status": "pass" if results and all(result["status"] == "pass" for result in results) else "fail",
        "scenarios": [
            {
                "scenario": result["scenario"], "status": result["status"],
                "failed_checks": [check["id"] for check in result["checks"] if check["status"] != "pass"],
                "stream_connected_ms": result["metrics"].get("stream_connected_ms"),
                "first_render_ms": result["metrics"].get("first_render_ms"),
                "live_frames": result["metrics"].get("live_frames"),
                "artifacts": result["artifacts"],
            }
            for result in results
        ],
    }
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    if results and results[0]["artifacts"].get("scratch"):
        root = Path(results[0]["artifacts"]["scratch"]) / "artifacts"
        root.mkdir(parents=True, exist_ok=True)
        (root / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def cmd_run(args: argparse.Namespace) -> None:
    state = load_state()
    names = args.scenario or list(SCENARIOS)
    unknown = set(names) - set(SCENARIOS)
    if unknown:
        die(f"unknown scenario(s): {', '.join(sorted(unknown))}; choose from {', '.join(SCENARIOS)}")
    results = []
    deploy = args.deploy
    for name in names:
        for key in ("session_id", "provider_session_id", "transcript", "render_expectation", "recovery", "screenshots",
                    "expected_assistant_text", "expected_assistant_uuid", "network_boundary"):
            state.pop(key, None)
        state["phase"] = f"start_{name}"
        save_state(state)
        log(f"=== scenario {name}")
        interrupted = False
        try:
            envelope = SCENARIOS[name](state, deploy)
            deploy = False
        except (Exception, KeyboardInterrupt) as exc:
            interrupted = isinstance(exc, KeyboardInterrupt)
            envelope = failure_verdict(state, name, exc)
        results.append(envelope)
        summary = write_summary(results)
        failed = [check["id"] for check in envelope["checks"] if check["status"] != "pass"]
        log(f"=== {name}: {envelope['status']}" + (f" ({', '.join(failed)})" if failed else ""))
        if interrupted:
            break
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
    run_parser.add_argument("scenario", nargs="*", metavar="SCENARIO", help=f"default: all; choices: {', '.join(SCENARIOS)}")
    run_parser.add_argument("--deploy", action="store_true", help="build and install the app before the first scenario")
    run_parser.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
