"""A fake Longhouse for the Pi-family Helm producers: the executables they launch and the Runtime Host they call.

The real producer ``main`` runs against it, so a change to a body's control flow or evidence shape shows without a provider.
It does NOT exercise shipping, ingest or the engine's parsers: the shipper commands do nothing and the host serves events
read straight from the fake provider's own files. A green run is no coverage of those.

It encodes a reading of the engine. Each behaviour taken from engine source names the file and function it was read
from, so it can be checked there. Run as a script it is both ``longhouse`` and ``longhouse-engine``.
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

# How long the scripted model spends in one tool call of a step task, and in the one tool call of a busy turn.
STEP_SECONDS = 0.6
BUSY_SECONDS = 2.0
# No fake process outlives this, whatever happened to the test that started it.
MAX_LIFETIME_SECONDS = 600
# Faults, comma separated: ``exit_after:<n>`` kills the launcher and its provider once the n-th turn has ended;
# ``steer_as_follow_up`` holds a steer until the turn it was sent into is over (the engine's own negative control,
# zerg.qa.pi_family_turn_oracle: NEGATIVE_CONTROLS).
FAULT_ENV = "LONGHOUSE_FAKE_HELM_FAULT"
_REPLY = re.compile(r"reply (?:only )?with (?:exactly )?([A-Za-z0-9_]+)", re.IGNORECASE)
_RECALL = re.compile(r"context phrase that starts with ([A-Z_]+_)\..*then ([A-Za-z0-9_]+), and nothing else", re.DOTALL)
_STEP_TASK = re.compile(r"Task (\S+): run each")
_ID_KEY = {"omp": "native_session_id", "pi": "provider_session_id"}


def _faults() -> set[str]:
    return set(os.environ.get(FAULT_ENV, "").split(","))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _birth(pid: int) -> str | None:
    """A process's start time as the producers read it (their ``_process_record`` runs this same ``ps``)."""

    fields = subprocess.run(
        ["ps", "-o", "pid=", "-o", "pgid=", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    parts = fields.stdout.strip().split(None, 2)
    return parts[2].strip() if len(parts) >= 3 else None


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _round_trip(socket_path: str, frame: dict[str, Any]) -> dict[str, Any]:
    """One command frame to a launcher and its one-line answer (engine/src/helm_control.rs: round_trip)."""

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(10)
        channel.connect(socket_path)
        channel.sendall((json.dumps(frame) + "\n").encode())
        answer = b""
        while not answer.endswith(b"\n"):
            chunk = channel.recv(65536)
            if not chunk:
                break
            answer += chunk
    if not answer:
        raise OSError("the channel closed without an answer")
    return json.loads(answer.decode())


def _command_frame(state: dict[str, Any], kind: str, text: str | None = None) -> dict[str, Any]:
    """The frame a control command is, built from a launcher's state (engine/src/pi_helm_control.rs: dispatch_with_attachments)."""

    id_key = _ID_KEY[state["provider"]]
    frame = {
        "kind": kind,
        "auth_token": state["channel_token"],
        "session_id": state["session_id"],
        id_key: state[id_key],
        "connection_id": state["connection_id"],
        "lease_generation": state["lease_generation"],
    }
    if text is not None:
        frame["text"] = text
    return frame


def _post(url: str, path: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}", data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=10).close()


# ---------------------------------------------------------------------------------------------------------------------
# The launcher: `longhouse omp|pi`, the process that owns one provider and its channel.


class _Launcher:
    def __init__(self, provider: str, *, url: str, cwd: str, model: str, resume_session: str | None) -> None:
        self.provider = provider
        self.id_key = _ID_KEY[provider]
        self.url = url
        self.model = model or "fake/model"
        home = Path(os.environ["LONGHOUSE_HOME"])
        self.state_dir = home / "managed-local" / f"{provider}-helm"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir = (
            Path(os.environ["LONGHOUSE_OMP_SESSION_DIR"]) if provider == "omp" else Path(os.environ["PI_CODING_AGENT_DIR"]) / "sessions"
        )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.prompts: queue.Queue[str] = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.steer: str | None = None
        self.held_steer: str | None = None
        self.abort = False
        self.terminate_requested = False
        self.turns = 0
        self.leaf: str | None = None
        prior: dict[str, Any] = {}
        if resume_session is not None:
            try:
                prior = json.loads((self.state_dir / f"{resume_session}.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise SystemExit(f"{provider} Helm session {resume_session} has no state to resume") from None
            # One session has one execution owner (engine/src/pi_helm_launcher.rs: launch_lock; omp_helm_launcher.rs: launch_lock_in).
            if prior.get("status") != "stopped" and _alive(prior.get("launcher_pid")):
                raise SystemExit(f"{provider} Helm session {resume_session} is already attached or resuming")
            self.leaf = self._last_entry_id(Path(prior["session_file"]))
        session_id = resume_session or str(uuid.uuid4())
        native_id = prior.get(self.id_key) or str(uuid.uuid4())
        self.provider_process = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({MAX_LIFETIME_SECONDS})"])
        self.socket_path = self.state_dir / f"{session_id[:8]}.sock"
        self.socket_path.unlink(missing_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        self.server.listen(8)
        # The state file a launcher keeps (engine/src/pi_helm_launcher.rs: PiHelmStateFile; omp_helm_launcher.rs: OmpHelmStateFile).
        self.state: dict[str, Any] = {
            "schema_version": 1,
            "session_id": session_id,
            "run_id": str(uuid.uuid4()),
            "provider": provider,
            "model": self.model,
            self.id_key: native_id,
            "session_file": prior.get("session_file") or self._new_session_file(native_id),
            "session_dir": str(self.session_dir),
            "cwd": cwd,
            "socket_path": str(self.socket_path),
            "channel_token": uuid.uuid4().hex,
            "connection_id": str(uuid.uuid4()),
            "lease_generation": str(uuid.uuid4()),
            "launcher_pid": os.getpid(),
            "launcher_process_start_time": _birth(os.getpid()),
            "provider_pid": self.provider_process.pid,
            "provider_process_start_time": _birth(self.provider_process.pid),
            "status": "ready",
            "phase": "idle",
            "ready": True,
            "agent_end_observed": False,
            "agent_end_is_terminal": None,
            "agent_end_is_terminal_present": False,
            "agent_end_will_continue": None,
            "agent_end_will_continue_present": False,
            "live_turn_seq": int(prior.get("live_turn_seq") or 0),
            "terminal_state": None,
            "terminal_reason": None,
            "exit_code": None,
            "started_at": _now(),
            "updated_at": _now(),
        }

    def _new_session_file(self, native_id: str) -> str:
        return str(self.session_dir / f"{datetime.now(UTC).strftime('%Y-%m-%dT%H-%M-%S-%f')}_{native_id}.jsonl")

    @staticmethod
    def _last_entry_id(session_file: Path) -> str | None:
        entries = [json.loads(line) for line in session_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        return next((entry["id"] for entry in reversed(entries) if entry.get("type") == "message"), None)

    def _publish(self, *, host: bool = False) -> None:
        """Write the state file; ``host`` first tells the Runtime Host, so it never serves an owner older than the file names."""

        with self.lock:
            self.state["updated_at"] = _now()
            if host:
                _post(self.url, "/__fake/owner", self.state)
            _write_json(self.state_dir / f"{self.state['session_id']}.json", self.state)

    # -- the provider's own transcript: one JSONL session tree (zerg.qa.pi_family_turn_oracle describes the shape)

    def _entry(self, message: dict[str, Any]) -> str:
        session_file = Path(self.state["session_file"])
        lines = []
        if not session_file.exists():
            # A replacement session's file appears with its first turn (the Pi body: "Pi reserves the path at /new").
            header = {"type": "session", "version": 3, "id": self.state[self.id_key], "timestamp": _now(), "cwd": self.state["cwd"]}
            lines.append(json.dumps(header))
        entry_id = uuid.uuid4().hex[:8]
        lines.append(json.dumps({"type": "message", "id": entry_id, "parentId": self.leaf, "timestamp": _now(), "message": message}))
        with session_file.open("a", encoding="utf-8") as stream:
            stream.write("".join(line + "\n" for line in lines))
        self.leaf = entry_id
        return entry_id

    def _assistant(self, content: list[dict[str, Any]], stop_reason: str) -> None:
        usage = {"input": 1, "output": 1}
        self._entry(
            {
                "role": "assistant",
                "content": content,
                "stopReason": stop_reason,
                "provider": "openrouter",
                "model": self.model,
                "usage": usage,
            }
        )

    def _tool_call(self, name: str, arguments: dict[str, Any], result: str, seconds: float) -> None:
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        self._assistant([{"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}], "toolUse")
        self.stop.wait(seconds)
        self._entry(
            {"role": "toolResult", "toolCallId": call_id, "toolName": name, "content": [{"type": "text", "text": result}], "isError": False}
        )

    # -- the scripted model

    def _reply(self, prompt: str) -> str:
        recall = _RECALL.search(prompt)
        if recall is not None:
            # Only restored history holds the phrase: read it back from this session's own file.
            history = Path(self.state["session_file"]).read_text(encoding="utf-8")
            phrase = re.search(re.escape(recall.group(1)) + r"[0-9a-f]+", history)
            return f"{phrase.group(0) if phrase else 'no phrase in history'} {recall.group(2)}"
        markers = _REPLY.findall(prompt)
        return markers[-1] if markers else "ok"

    def _step_task(self, task_marker: str) -> str | None:
        """Six tool calls, one at a time. A steer or an abort lands at a tool boundary; what it leaves is returned."""

        for step in range(1, 7):
            self._tool_call("bash", {"command": f"sleep 4 && echo {task_marker}-step{step}"}, f"{task_marker}-step{step}\n", STEP_SECONDS)
            with self.lock:
                if self.abort or self.stop.is_set():
                    return "aborted"
                if self.steer is not None:
                    steer, self.steer = self.steer, None
                    self._entry({"role": "user", "content": [{"type": "text", "text": steer}]})
                    return steer
        return None

    def _turn(self, prompt: str) -> None:
        self._entry({"role": "user", "content": [{"type": "text", "text": prompt}]})
        task = _STEP_TASK.search(prompt)
        if task is not None:
            left = self._step_task(task.group(1))
            if left == "aborted":
                self._assistant([{"type": "text", "text": ""}], "aborted")
            else:
                self._assistant([{"type": "text", "text": self._reply(left or prompt)}], "stop")
        else:
            read = re.search(r"Use the read tool (?:repeatedly on|to read) (\S+?),", prompt)
            if read is not None:
                seconds = BUSY_SECONDS if "repeatedly" in prompt else 0.1
                self._tool_call("read", {"path": read.group(1)}, Path(read.group(1)).read_text(encoding="utf-8"), seconds)
            elif "Use the bash tool" in prompt:
                self._tool_call("bash", {"command": "sleep 8"}, "", BUSY_SECONDS)
            self._assistant([{"type": "text", "text": self._reply(prompt)}], "stop")
        with self.lock:
            self.busy, self.steer, self.abort = False, None, False
            # What the OMP extension reports when a turn ends (engine/src/omp_helm_launcher.rs: handle_extension_frame, "agent_end").
            self.state.update(
                phase="idle",
                agent_end_observed=True,
                agent_end_is_terminal=True,
                agent_end_is_terminal_present=True,
                agent_end_will_continue=False,
                agent_end_will_continue_present=True,
            )
            self._publish()
        self.turns += 1
        if f"exit_after:{self.turns}" in _faults():
            # The provider and its launcher die with no terminal state written, as a crash leaves them.
            self.provider_process.kill()
            self.provider_process.wait()
            os._exit(70)
        if self.held_steer is not None:
            held, self.held_steer = self.held_steer, None
            self._accept(held)

    def _accept(self, prompt: str) -> None:
        with self.lock:
            self.busy = True
            self.state.update(phase="running", agent_end_observed=False, live_turn_seq=self.state["live_turn_seq"] + 1)
            self._publish()
        self.prompts.put(prompt)

    # -- the channel

    def _command(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Answer one command frame (engine/src/pi_helm_launcher.rs and omp_helm_launcher.rs: handle_remote_command)."""

        def refused(code: str) -> dict[str, Any]:
            return {"ok": False, "error": {"code": code, "message": f"{self.provider} Helm refused: {code}"}}

        with self.lock:
            state = self.state
            # Same file: remote_authority_matches_locked. A frame from an owner that is no longer current is stale.
            if not (
                state["ready"]
                and frame.get("auth_token") == state["channel_token"]
                and all(frame.get(name) == state[name] for name in ("session_id", self.id_key, "connection_id", "lease_generation"))
            ):
                return refused("stale_channel")
            kind = frame.get("kind")
            if kind == "send":
                # A busy provider does not take a send (engine/assets/longhouse-pi-helm.ts and longhouse-omp-helm.ts: "turn_active").
                if self.busy:
                    return refused("turn_active")
                self._accept(str(frame.get("text") or ""))
            elif kind == "steer":
                if not self.busy:
                    return refused("turn_ended")
                if "steer_as_follow_up" in _faults():
                    self.held_steer = str(frame.get("text") or "")
                else:
                    self.steer = str(frame.get("text") or "")
            elif kind == "abort":
                self.abort = self.busy
            elif kind == "terminate":
                self.terminate_requested = True
                self.stop.set()
            else:
                return refused("bad_request")
            return {"kind": "command_result", "ok": True, "status": "active" if self.busy else "idle", self.id_key: state[self.id_key]}

    def _serve_channel(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _address = self.server.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                try:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    connection.sendall((json.dumps(self._command(json.loads(data.decode() or "{}"))) + "\n").encode())
                except (OSError, json.JSONDecodeError):
                    continue

    def _read_terminal(self) -> None:
        for line in sys.stdin:
            command = line.strip()
            with self.lock:
                if command.endswith("/new"):
                    # A new native session under the same Longhouse session; OMP's switch also rotates the lease
                    # (engine/src/omp_helm_launcher.rs: handle_extension_frame, "session_before_switch").
                    native_id = str(uuid.uuid4())
                    self.state.update({self.id_key: native_id, "session_file": self._new_session_file(native_id)})
                    self.leaf = None
                    if self.provider == "omp":
                        self.state["lease_generation"] = str(uuid.uuid4())
                    self._publish(host=True)
                elif command.endswith("/reload"):
                    # The extension reconnects: a new connection and a new lease (engine/src/pi_helm_launcher.rs: handle_extension).
                    self.state.update(connection_id=str(uuid.uuid4()), lease_generation=str(uuid.uuid4()))
                    self._publish(host=True)

    def _watch(self) -> None:
        parent, deadline = os.getppid(), time.monotonic() + MAX_LIFETIME_SECONDS
        while not self.stop.wait(1.0):
            if os.getppid() != parent or time.monotonic() > deadline:
                self.stop.set()

    def run(self, prompt: str) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGHUP, lambda *_: self.stop.set())
        try:
            self._publish(host=True)
            for target in (self._serve_channel, self._read_terminal, self._watch):
                threading.Thread(target=target, daemon=True).start()
            self._accept(prompt)
            while not self.stop.is_set():
                try:
                    self._turn(self.prompts.get(timeout=0.05))
                except queue.Empty:
                    continue
        finally:
            self.provider_process.kill()
            self.provider_process.wait()
            self.server.close()
            self.socket_path.unlink(missing_ok=True)
            # How a launcher records its end (engine/src/pi_helm_launcher.rs and omp_helm_launcher.rs: launch).
            reason = "remote_terminate" if self.terminate_requested else "terminal_signal"
            self.state.update(status="stopped", ready=False, phase="idle", terminal_state="ended", terminal_reason=reason, exit_code=0)
            try:
                self._publish(host=True)
            except OSError:
                self._publish()
        return 0


# ---------------------------------------------------------------------------------------------------------------------
# The command line: what the producers run as `--longhouse-cli` and `--engine`.


def _option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv: list[str]) -> int:
    command, rest = argv[0], argv[1:]
    if command in {"omp", "pi"}:
        # `longhouse <provider>` is not the launcher: the launcher is its child, the engine. The Pi body reads the
        # recorded launcher dead before it reaps the process it started on the terminal, so they cannot be one process.
        return subprocess.call([sys.executable, __file__, "launch", command, *rest])
    if command == "launch":
        provider = rest[0]
        url = _option(rest, "--url") or os.environ[f"LONGHOUSE_{provider.upper()}_HELM_URL"]
        launcher = _Launcher(
            provider,
            url=url,
            cwd=_option(rest, "--cwd") or os.getcwd(),
            model=_option(rest, "--model") or "",
            resume_session=_option(rest, "--resume-session"),
        )
        return launcher.run(_option(rest, "--prompt") or "")
    if command == "pi-helm":
        # engine/src/main.rs: Commands::PiHelm prints this object when the launcher took the command.
        state = json.loads(
            (Path(os.environ["LONGHOUSE_HOME"]) / "managed-local" / "pi-helm" / f"{_option(rest, '--session-id')}.json").read_text()
        )
        try:
            answer = _round_trip(state["socket_path"], _command_frame(state, rest[0], _option(rest, "--text")))
        except OSError as exc:
            answer = {"ok": False, "error": {"code": "session_not_attached", "message": str(exc)}}
        if answer.get("ok") is not True:
            print(json.dumps(answer.get("error")), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "provider_session_id": answer.get("provider_session_id"), "status": answer.get("status")}))
        return 0
    if command == "connect":
        # The Machine Agent is ready when its wake socket exists (engine/src/codex_bridge.rs names the path). It ships nothing here.
        _post(str(_option(rest, "--url")), "/__fake/process", {"pid": os.getpid()})
        (Path(str(_option(rest, "--db"))).parent / "transcript-wake.sock").touch()
        time.sleep(MAX_LIFETIME_SECONDS)
        return 0
    if command == "ship":
        print(json.dumps({"protocol": "fake", "files_scanned": 0, "files_shipped": 0, "events_shipped": 0, "spool_replayed": 0}))
        return 0
    if command == "local-health":
        print(json.dumps({"status": "ok", "fake": True}))
        return 0
    print(f"the fake longhouse has no command {command!r}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------------------------------------------------
# The Runtime Host. Its answers have the shape of server/zerg/routers/session_chat.py and agents_state_diagnostics.py.


class FakeRuntimeHost:
    """The agents API the producers call, over the owners the fake launchers register."""

    def __init__(self, token: str, *, reconnecting: int = 0, never_hides: bool = False) -> None:
        """``reconnecting`` control requests are first refused as a control channel that is reconnecting;
        ``never_hides`` leaves a session visible however it is retired (a cleanup that cannot be proven)."""

        self.token = token
        self.reconnecting = reconnecting
        self.never_hides = never_hides
        self.sessions: dict[str, dict[str, Any]] = {}
        self.processes: set[int] = set()
        self.lock = threading.RLock()
        self._closed = threading.Event()
        host = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def _answer(self, status: int, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                path = self.path.split("?", 1)[0]
                if not path.startswith("/__fake/") and self.headers.get("X-Agents-Token") != host.token:
                    return self._answer(401, {"detail": "bad token"})
                status, payload = host.route(self.command, path, body)
                self._answer(status, payload)

            do_GET = do_POST = do_PATCH = _handle

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    # -- what the launchers tell it

    def _register(self, state: dict[str, Any]) -> None:
        with self.lock:
            session = self.sessions.setdefault(state["session_id"], {"files": [], "queue": [], "hidden": False, "archived": False})
            session["owner"] = state
            if state["session_file"] not in session["files"]:
                session["files"].append(state["session_file"])
            self.processes.update((state["launcher_pid"], state["provider_pid"]))

    def _command(self, session: dict[str, Any], kind: str, text: str | None = None) -> dict[str, Any]:
        owner = session["owner"]
        try:
            return _round_trip(owner["socket_path"], _command_frame(owner, kind, text))
        except OSError as exc:
            return {"ok": False, "error": {"code": "session_not_attached", "message": str(exc)}}

    def _drain(self) -> None:
        """Deliver a queued send at the turn boundary: the launcher takes it once the provider is idle."""

        while not self._closed.wait(0.1):
            with self.lock:
                waiting = [session for session in self.sessions.values() if session["queue"]]
            for session in waiting:
                if self._command(session, "send", session["queue"][0]).get("ok") is True:
                    session["queue"].pop(0)

    # -- what it serves

    @staticmethod
    def _events(session_id: str, session: dict[str, Any]) -> list[dict[str, Any]]:
        """Every text message of the provider's own files, served under the id the provider gave it."""

        events = []
        for name in session["files"]:
            try:
                lines = Path(name).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:  # a row the provider is still writing
                    continue
                message = entry.get("message") or {}
                text = "\n".join(block.get("text") or "" for block in message.get("content") or [] if block.get("type") == "text")
                if message.get("role") in {"user", "assistant"} and text:
                    event = {"id": entry["id"], "session_id": session_id, "role": message["role"], "content_text": text}
                    events.append({**event, "event_origin": "durable", "tool_name": None})
        return events

    def route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, object]:
        if path == "/__fake/owner":
            self._register(body)
            return 200, {}
        if path == "/__fake/process":
            with self.lock:
                self.processes.add(body["pid"])
            return 200, {}
        if path == "/api/agents/storage/v2/capabilities":
            return 200, {"machine_id": "fake-machine"}
        if path == "/api/agents/sessions":
            with self.lock:
                listed = [{"id": session_id} for session_id, session in self.sessions.items() if not session["archived"]]
            return 200, {"sessions": listed, "total": len(listed)}
        found = re.fullmatch(r"/api/agents/sessions/([^/]+)(?:/([a-z-]+))?", path)
        with self.lock:
            session = self.sessions.get(found.group(1)) if found else None
        if found is None or session is None:
            return 404, {"detail": "no such session"}
        session_id, action, owner = found.group(1), found.group(2), session["owner"]
        if action is None:
            return 200, {"id": session_id, "provider": owner["provider"], "provider_session_id": owner[_ID_KEY[owner["provider"]]]}
        if action == "workspace":
            return 200, {"session_id": session_id, "items": []}
        if action == "thread":
            return 200, {"root_session_id": session_id, "head_session_id": session_id, "sessions": [{"id": session_id}]}
        if action == "events":
            events = self._events(session_id, session)
            page = {"session_id": session_id, "generation_id": "fake-generation", "branch_mode": "head", "events": events}
            return 200, {**page, "has_more": False, "next_cursor": None, "total": len(events)}
        if action == "state-diagnostics":
            stopped = owner["status"] == "stopped" or not _alive(owner["launcher_pid"])
            available = {"state": "available"}
            shadow = {
                "mode": "fake",
                "fact_sources": {"control": {"subject_key": f"connection:{owner['connection_id']}:{owner['lease_generation']}"}},
                "control": {"actions": {"send_input": available, "interrupt": available, "terminate": available}},
                "run": {"id": owner["run_id"], "lifecycle": "stopped" if stopped else "running"},
                "activity": {"state": "idle" if stopped else "unknown"},
            }
            return 200, {"session_id": session_id, "served_path": "canonical_session_detail", "shadow": shadow}
        if action == "timeline-visibility":
            session["hidden"] = body.get("hidden") is True and not self.never_hides
            return 200, {"hidden": session["hidden"]}
        if action == "action":
            session["archived"] = body.get("action") == "archive"
            return 200, {"user_state": "archived" if session["archived"] else "active"}
        if action in {"input", "send-live", "interrupt-live", "terminate-live"} and self.reconnecting > 0:
            # What the Runtime Host answers while the Machine Agent's control channel re-registers
            # (zerg.qa.pi_family_helm: _control_channel_reconnecting).
            self.reconnecting -= 1
            return 409, {"detail": "no live Longhouse control channel for this session"}
        if action == "input":
            steer = body.get("intent") == "steer"
            answer = self._command(session, "steer" if steer else "send", body.get("text"))
            outcome = "sent" if answer.get("ok") is True else None
            if outcome is None and not steer and (answer.get("error") or {}).get("code") == "turn_active":
                # A send to a busy provider is kept and delivered at the turn boundary: the answer is "queued".
                session["queue"].append(body.get("text"))
                outcome = "queued"
            if outcome is None:
                return 502, {"detail": {"error_code": "input_failed", "message": json.dumps(answer.get("error"))}}
            return 200, {"outcome": outcome, "client_request_id": body.get("client_request_id")}
        commands = {
            "send-live": ("send", "accepted"),
            "interrupt-live": ("abort", "interrupt_dispatched"),
            "terminate-live": ("terminate", "terminate_dispatched"),
        }
        if action in commands:
            kind, field = commands[action]
            answer = self._command(session, kind, body.get("message"))
            if answer.get("ok") is not True:
                return 502, {"detail": {"error_code": f"{kind}_failed", "message": json.dumps(answer.get("error"))}}
            return 200, {field: True, "session_id": session_id}
        return 404, {"detail": f"the fake Runtime Host does not serve {method} {path}"}

    def close(self) -> list[int]:
        """Stop serving and kill whatever fake process the run left alive. Returns the pids it had to kill."""

        self._closed.set()
        self.server.shutdown()
        self.server.server_close()
        left = [pid for pid in sorted(self.processes) if _alive(pid)]
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return left


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
