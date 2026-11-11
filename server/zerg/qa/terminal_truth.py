"""Terminal truth baseline, track A: how long after text is on the terminal is it served?

The terminal is the reference. A provider CLI runs inside a PTY this module owns, pointed
at the scripted model endpoint, which streams a long paced reply with a unique marker word
every N tokens. Each marker has two times on ONE clock (``time.monotonic()``, which every
process on this host shares):

- terminal: the first PTY chunk after which the marker is on a pyte-emulated screen
  (``zerg.qa.terminal_screen``), from the timestamped recording (``zerg.qa.pty_reader``);
- lane: the first response from a Runtime Host lane that contains the marker. A lane is
  polled (durable events, the web workspace) or streamed (the workspace SSE).

``delay = lane time - terminal time``. A marker on screen that no lane ever served is
reported as never served. The marker list is the curve; the table is first, median, worst.

Resolution is part of every number. A polled lane records the time its response arrived,
so its error is at most one poll gap (interval plus request time, reported per lane); the
terminal side is one PTY read. A 100 ms poll cannot claim 5 ms, and the report says so.

The check on the harness itself is ``selfcheck``: a fake TUI prints markers at truth-logged
times with spinner and input-box redraws (the schedule must be recovered), and two stub lanes
serve the same markers promptly and only at the end of the stream (the second must be
reported late by about the stream length).

Seam for the next step: a lane is ``Lane`` (a name plus a function that returns the lane's
current text); a browser-paint or iOS lane is another ``Lane``.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from zerg.qa import scripted_model_profiles
from zerg.qa.pty_reader import TimedChunk
from zerg.qa.pty_reader import load_timed_chunks
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.scripted_model_server import REASONING_WORD
from zerg.qa.scripted_model_server import Pacing
from zerg.qa.scripted_model_server import pace_schedule
from zerg.qa.scripted_model_server import paced_text
from zerg.qa.scripted_model_server import parse_pacing
from zerg.qa.terminal_screen import ScreenReplay

SERVER_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER_ROOT.parent
# Short on purpose: managed Helm binds unix sockets under HOME and macOS caps a socket path at 104 bytes.
SCRATCH_ROOT = Path("/tmp/agents/tt")
ROWS, COLUMNS = 40, 132
PROMPT = "TTRUTH please stream the long reply"


# --------------------------------------------------------------------------------------
# Delay arithmetic (pure)
# --------------------------------------------------------------------------------------


@dataclass
class LaneStats:
    """One lane's delay curve for one run."""

    lane: str
    resolution_s: float | None  # worst gap between consecutive observations (poll gap); None for a stream
    delays: dict[str, float | None]  # marker -> lane time - terminal time; None: on screen, never served
    observations: int = 0  # responses (polled) or lines (stream) the lane produced during the run
    first: float | None = None
    median: float | None = None
    worst: float | None = None
    never_served: list[str] = field(default_factory=list)
    ship_lag: dict[str, float | None] = field(default_factory=dict)  # marker -> lane time - provider file write time
    ship_first: float | None = None
    ship_median: float | None = None
    ship_worst: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "resolution_s": self.resolution_s,
            "observations": self.observations,
            "first_marker_delay_s": self.first,
            "median_delay_s": self.median,
            "worst_delay_s": self.worst,
            "never_served": self.never_served,
            "delays_s": self.delays,
            "ship_lag_s": self.ship_lag,
            "ship_lag_first_s": self.ship_first,
            "ship_lag_median_s": self.ship_median,
            "ship_lag_worst_s": self.ship_worst,
        }


def lane_stats(
    lane: str,
    terminal: dict[str, float | None],
    served: dict[str, float],
    resolution_s: float | None,
    order: Sequence[str],
    file_written: dict[str, float] | None = None,
) -> LaneStats:
    """Delay per marker that reached the terminal. Markers absent from the terminal are not scored."""

    delays: dict[str, float | None] = {}
    for marker in order:
        seen = terminal.get(marker)
        if seen is None:
            continue
        delays[marker] = served[marker] - seen if marker in served else None
    numbers = [d for d in delays.values() if d is not None]
    ship: dict[str, float | None] = {}
    for marker in delays:
        if file_written is not None and marker in file_written:
            ship[marker] = served[marker] - file_written[marker] if marker in served else None
    ship_numbers = [d for d in ship.values() if d is not None]
    return LaneStats(
        ship_lag=ship,
        ship_first=ship.get(order[0]) if order else None,
        ship_median=statistics.median(ship_numbers) if ship_numbers else None,
        ship_worst=max(ship_numbers) if ship_numbers else None,
        lane=lane,
        resolution_s=resolution_s,
        delays=delays,
        first=delays[order[0]] if order and order[0] in delays else None,
        median=statistics.median(numbers) if numbers else None,
        worst=max(numbers) if numbers else None,
        never_served=[m for m, d in delays.items() if d is None],
    )


def terminal_first_seen(chunks: Iterable[TimedChunk | tuple[float, bytes]], markers: Sequence[str]) -> dict[str, float | None]:
    replay = ScreenReplay(markers, rows=ROWS, columns=COLUMNS)
    for chunk in chunks:
        t, data = (chunk.t, chunk.data) if isinstance(chunk, TimedChunk) else chunk
        replay.feed(t, data)
    return {r.needle: r.t for r in replay.result(markers)}


# --------------------------------------------------------------------------------------
# Lanes: a name and a way to read the lane's current text
# --------------------------------------------------------------------------------------


@dataclass
class Lane:
    """A served surface that can be asked "what do you show now". Polled lanes set ``interval``."""

    name: str
    read: Callable[[], str | None]  # text now, or None when the surface does not exist yet
    interval: float = 0.05


class LaneWatcher(threading.Thread):
    """Poll a lane until every marker is served or ``stop`` is set; record first-seen times."""

    def __init__(self, lane: Lane, markers: Sequence[str], stop: threading.Event) -> None:
        super().__init__(daemon=True, name=f"lane-{lane.name}")
        self.lane = lane
        self.markers = list(markers)
        self.first_seen: dict[str, float] = {}
        self.response_times: list[float] = []
        self.errors: list[str] = []
        self._stop_event = stop

    @property
    def resolution_s(self) -> float | None:
        gaps = [b - a for a, b in zip(self.response_times, self.response_times[1:], strict=False)]
        return max(gaps) if gaps else None

    def run(self) -> None:
        pending = set(self.markers)
        while pending and not self._stop_event.is_set():
            try:
                text = self.lane.read()
            except Exception as exc:  # noqa: BLE001 - a lane failure is data, not a crash
                text = None
                if len(self.errors) < 20:
                    self.errors.append(f"{type(exc).__name__}: {exc}")
            now = time.monotonic()
            if text is not None or not self.errors:
                self.response_times.append(now)  # "the surface does not exist yet" is an answer too
            if text is not None:
                for marker in [m for m in pending if m in text]:
                    self.first_seen[marker] = now
                    pending.discard(marker)
            if pending:
                self._stop_event.wait(self.lane.interval)


class FileWatcher(threading.Thread):
    """When a marker first appears in a file under the provider's HOME (its transcript, session file or store).

    Walks the tree every ``interval`` seconds, reads files whose size or mtime changed, and searches them for the markers.
    Resolution is the worst gap between completed scans (reported). The Machine Agent's own state (``.longhouse``) is
    excluded: this watches what the provider wrote, not what Longhouse did with it.
    """

    SKIP_DIRS = {".longhouse", "node_modules", ".git", "bin"}

    def __init__(
        self, root: Path, markers: Sequence[str], stop: threading.Event, interval: float = 0.03, only_under: Sequence[str] = ()
    ) -> None:
        super().__init__(daemon=True, name="provider-files")
        self.root = root
        self.only_under = tuple(only_under)  # when set, only files below these HOME-relative prefixes count
        self.markers = list(markers)
        self.first_seen: dict[str, float] = {}
        self.first_path: dict[str, str] = {}
        self.scan_times: list[float] = []
        self.interval = interval
        self._stop_event = stop
        self._stat: dict[str, tuple[int, int]] = {}

    @property
    def resolution_s(self) -> float | None:
        gaps = [b - a for a, b in zip(self.scan_times, self.scan_times[1:], strict=False)]
        return max(gaps) if gaps else None

    def scan_once(self) -> None:
        pending = [m for m in self.markers if m not in self.first_seen]
        for directory, names, files in os.walk(self.root):
            names[:] = [n for n in names if n not in self.SKIP_DIRS]
            for name in files:
                path = os.path.join(directory, name)
                if self.only_under and not os.path.relpath(path, self.root).startswith(self.only_under):
                    continue
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                key = (info.st_mtime_ns, info.st_size)
                if self._stat.get(path) == key or info.st_size > 64 * 1024 * 1024 or not pending:
                    self._stat[path] = key
                    continue
                self._stat[path] = key
                try:
                    data = Path(path).read_bytes()
                except OSError:
                    continue
                now = time.monotonic()
                for marker in [m for m in pending if m.encode() in data]:
                    self.first_seen[marker] = now
                    self.first_path[marker] = os.path.relpath(path, self.root)
                pending = [m for m in pending if m not in self.first_seen]

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.scan_once()
            self.scan_times.append(time.monotonic())
            self._stop_event.wait(self.interval)


class StreamWatcher(threading.Thread):
    """Read a server-sent-event stream; a marker's time is when the frame carrying it arrived."""

    def __init__(self, name: str, open_stream: Callable[[], Iterator[str]], markers: Sequence[str], stop: threading.Event) -> None:
        super().__init__(daemon=True, name=f"lane-{name}")
        self.lane = Lane(name, lambda: None)
        self.open_stream = open_stream
        self.markers = list(markers)
        self.first_seen: dict[str, float] = {}
        self.errors: list[str] = []
        self.resolution_s: float | None = None
        self.response_times: list[float] = []
        self.event_counts: dict[str, int] = {}
        self.sample: list[str] = []  # the first stream lines, to see what the lane carries when no marker arrives
        self.final_preview: dict[str, str] = {}  # session id -> the last transcript_preview text seen on the stream
        self.previews: dict[str, list[str]] = {}  # session id -> each `transcript_preview` text, in arrival order
        self._stop_event = stop

    def _record_preview(self, data: str) -> None:
        try:
            frame = json.loads(data)
        except ValueError:
            return
        preview = frame.get("transcript_preview")
        if isinstance(preview, dict) and not preview.get("tool_name") and isinstance(preview.get("text"), str):
            self.previews.setdefault(str(frame.get("session_id")), []).append(preview["text"])

    def run(self) -> None:
        pending = set(self.markers)
        try:
            for line in self.open_stream():
                now = time.monotonic()
                self.response_times.append(now)
                text = line.strip()
                if text.startswith("event:"):
                    self.event_counts[text[6:].strip()] = self.event_counts.get(text[6:].strip(), 0) + 1
                elif text.startswith("data:") and "workspace_changed" not in text and '"session_id"' in text and len(self.sample) < 12:
                    self.sample.append(text[:400])
                if text.startswith("data:") and '"transcript_preview": {' in text:
                    self._record_preview(text[5:])
                for marker in [m for m in pending if m in line]:
                    self.first_seen[marker] = now
                    pending.discard(marker)
                if text.startswith("data:") and '"transcript_preview": {' in text:
                    with contextlib.suppress(ValueError):
                        frame = json.loads(text[5:])
                        preview = frame.get("transcript_preview")
                        if isinstance(preview, dict) and isinstance(preview.get("text"), str):
                            self.final_preview[str(frame.get("session_id"))] = preview["text"]
                # Keep reading after the last marker: the final preview is the reply as the web client ends up showing it.
                if self._stop_event.is_set():
                    return
        except Exception as exc:  # noqa: BLE001
            if not self._stop_event.is_set():
                self.errors.append(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------------------
# Runtime Host and its API as the lanes see it
# --------------------------------------------------------------------------------------


class RuntimeApi:
    """The slice of the Runtime Host API the lanes read, over one keep-alive connection each."""

    def __init__(self, api_url: str, token: str) -> None:
        parsed = urlsplit(api_url)
        self.host, self.port = parsed.hostname or "127.0.0.1", parsed.port or 80
        self.token = token
        self.pinned_sessions: list[str] = []

    def _headers(self, browser: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "longhouse-terminal-truth/1"}
        headers["Authorization" if browser else "X-Agents-Token"] = f"Bearer {self.token}" if browser else self.token
        return headers

    def get(self, path: str, *, browser: bool = False, timeout: float = 10) -> tuple[int, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request("GET", path, headers=self._headers(browser))
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()

    def post_raw(self, path: str, payload: dict[str, Any]) -> tuple[int, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            headers = {**self._headers(False), "Content-Type": "application/json"}
            connection.request("POST", path, json.dumps(payload), headers)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self.post_raw(path, payload)
        if status >= 300:
            raise RuntimeError(f"POST {path}: HTTP {status} {body[:300]}")
        return json.loads(body)

    def session_ids(self) -> list[str]:
        """Every session the host has. A Helm launch creates a managed session beside the file-discovered one, and only
        one of them carries the live text, so a lane reads all of them."""

        status, body = self.get("/api/agents/sessions?limit=20")
        listed = [str(item["id"]) for item in json.loads(body).get("sessions") or []] if status == 200 else []
        return listed + [s for s in self.pinned_sessions if s not in listed]

    def _lane(self, name: str, path: str, *, browser: bool, interval: float) -> Lane:
        def read() -> str | None:
            sessions = self.session_ids()
            if not sessions:
                return None
            bodies = []
            for session in sessions:
                status, body = self.get(path.format(id=session), browser=browser)
                if status != 200:
                    raise RuntimeError(f"{name}: HTTP {status} {body[:200]}")
                bodies.append(body)
            return "\n".join(bodies)

        return Lane(name, read, interval)

    def poll_lanes(self) -> list[Lane]:
        return [
            self._lane("events (durable archive)", "/api/agents/sessions/{id}/events?limit=1000", browser=False, interval=0.05),
            self._lane("workspace (served to web)", "/api/timeline/sessions/{id}/workspace", browser=True, interval=0.1),
        ]

    def _stream_one(self, session: str, out: queue.Queue[str], stop: threading.Event) -> None:
        while not stop.is_set():
            connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
            try:
                headers = self._headers(True)
                headers["Accept"] = "text/event-stream"
                connection.request("GET", f"/api/timeline/sessions/{session}/workspace/stream", headers=headers)
                response = connection.getresponse()
                if response.status != 200:
                    out.put(f"harness: workspace stream HTTP {response.status} for {session}\n")
                    return
                while not stop.is_set():
                    raw = response.readline()
                    if not raw:
                        break
                    out.put(raw.decode("utf-8", errors="replace"))
            except OSError:
                pass
            finally:
                connection.close()
            stop.wait(0.2)

    def stream_lines(self, stop: threading.Event) -> Iterator[str]:
        """The workspace SSE (what the web client subscribes to) of every session, as lines, as each session appears."""

        out: queue.Queue[str] = queue.Queue()
        started: set[str] = set()
        last_scan = 0.0
        while not stop.is_set():
            if time.monotonic() - last_scan > 0.1:
                last_scan = time.monotonic()
                for session in self.session_ids():
                    if session not in started:
                        started.add(session)
                        threading.Thread(target=self._stream_one, args=(session, out, stop), daemon=True).start()
            try:
                yield out.get(timeout=0.05)
            except queue.Empty:
                continue


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


class ScratchRuntimeHost:
    """A loopback ``longhouse-server serve`` on its own database and HOME. Auth is off on loopback."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.process: subprocess.Popen[bytes] | None = None
        self.url = ""
        self.token = ""

    def start(self) -> None:
        home = self.root / "host-home"
        home.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        environment = scripted_model_profiles.credential_free_environment(reject_ambient_credentials=False)
        environment.update(HOME=str(home), LLM_DISABLED="1", SKIP_DEMO_SEED="1", ENVIRONMENT="test:e2e")
        executable = Path(sys.executable).parent / "longhouse-server"
        log = (self.root / "runtime-host.log").open("wb")
        self.process = subprocess.Popen(
            [str(executable), "serve", "--port", str(port), "--db", f"sqlite:///{self.root / 'runtime-host.db'}"],
            cwd=SERVER_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"scratch Runtime Host exited ({self.process.returncode}); see {self.root / 'runtime-host.log'}")
            with contextlib.suppress(OSError, urllib.error.URLError):
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=2):
                    pass
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("scratch Runtime Host did not answer /api/health")
        minted = _post_json(f"{self.url}/api/devices/tokens", {"device_id": "terminal-truth", "name": "terminal-truth"})
        self.token = str(minted["token"])

    def stop(self) -> None:
        _stop_group(self.process)


def _stop_group(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


class ScriptedModel:
    """The scripted model server as a child process serving one paced turn."""

    def __init__(self, root: Path, pace: Pacing, match: str, match_scope: str = "latest") -> None:
        self.root = root
        self.script = root / "scripted-model.json"
        self.script.write_text(
            json.dumps(
                {
                    "turns": [
                        {
                            "id": "paced",
                            "match": match,
                            "match_scope": match_scope,
                            "pace": {
                                "tokens": pace.tokens,
                                "rate": pace.rate,
                                "initial_delay": pace.initial_delay,
                                "marker_every": pace.marker_every,
                                "marker_prefix": pace.marker_prefix,
                                "reasoning_tokens": pace.reasoning_tokens,
                            },
                        }
                    ]
                }
            )
        )
        self.process: subprocess.Popen[bytes] | None = None
        self.endpoint = ""

    def start(self) -> None:
        port_file = self.root / "model.port"
        environment = dict(os.environ, PYTHONPATH=str(SERVER_ROOT))
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "zerg.qa.scripted_model_server",
                "--port-file",
                str(port_file),
                "--script",
                str(self.script),
                "--receipt-file",
                str(self.root / "model-receipt.json"),
                "--stream-log",
                str(self.root / "model-stream.jsonl"),
            ],
            cwd=SERVER_ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not port_file.exists():
            raise RuntimeError("scripted model server did not publish a port")
        self.endpoint = f"http://127.0.0.1:{port_file.read_text().strip()}"

    def stop(self) -> None:
        _stop_group(self.process)

    def sent_times(self, markers: Sequence[str]) -> dict[str, float | None]:
        """When the model server wrote the first frame carrying each marker to the provider (monotonic)."""

        seen: dict[str, float | None] = dict.fromkeys(markers)
        log = self.root / "model-stream.jsonl"
        if log.exists():
            for line in log.read_text().splitlines():
                row = json.loads(line)
                for marker in markers:
                    if seen[marker] is None and marker in row["frame"]:
                        seen[marker] = row["t"]
        return seen

    def served_requests(self) -> int:
        with contextlib.suppress(OSError, ValueError, KeyError):
            return int(json.loads((self.root / "model-receipt.json").read_text())["scripted_model"]["served_requests"])
        return 0


# --------------------------------------------------------------------------------------
# Providers: how each CLI is launched in a test-owned PTY
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDriver:
    provider: str
    binary: str
    ready_text: str  # on screen when the TUI accepts a prompt
    argv: Callable[[Path, Path], list[str]]  # (binary, workspace) -> argv
    answers: tuple[tuple[str, bytes], ...] = ()  # (screen text, keys): startup prompts the harness answers once


PROVIDERS: dict[str, ProviderDriver] = {
    "claude": ProviderDriver(
        "claude",
        "claude",
        'Try "',
        lambda b, w: [str(b)],
        answers=(("I am using this for local development", b"\r"),),  # Helm's development-channel warning
    ),
    "codex": ProviderDriver(
        "codex",
        "codex",
        "Ask Codex",
        lambda b, w: [str(b), "--no-alt-screen", "--ask-for-approval", "never", "--sandbox", "read-only", "-C", str(w)],
    ),
    "opencode": ProviderDriver(
        "opencode",
        "opencode",
        "ctrl+p commands",
        lambda b, w: [str(b), "--model", "longhouse-mock/mock-model", str(w), "--pure"],
    ),
    "pi": ProviderDriver("pi", "pi", "", lambda b, w: [str(b), "--provider", "longhouse-mock", "--model", "mock-model"]),
    "omp": ProviderDriver("omp", "omp", "", lambda b, w: [str(b), "--model", "longhouse-mock/mock-model"]),
    "antigravity": ProviderDriver(
        "antigravity", "agy", "Gemini API key", lambda b, w: [str(b)], answers=(("Yes, I trust this folder", b"\r"),)
    ),
}


def provider_environment(provider: str, endpoint: str, script: Path, home: Path) -> dict[str, str]:
    """A credential-free environment for ``provider`` whose only reachable model is the scripted endpoint at ``endpoint``."""

    profiles = scripted_model_profiles
    saved = {k: os.environ.get(k) for k in (profiles.SCRIPTED_MODEL_BASE_URL_ENV, profiles.SCRIPTED_MODEL_SCRIPT_ENV)}
    os.environ[profiles.SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
    os.environ[profiles.SCRIPTED_MODEL_SCRIPT_ENV] = str(script)
    try:
        if provider in profiles.SCRIPTED_MODEL_API_KEY_BY_PROVIDER:
            environment = profiles.scripted_model_environment(provider)
        else:
            environment = profiles.credential_free_environment(reject_ambient_credentials=False)
            environment[profiles.SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    for name in [
        n
        for n in environment
        if n.startswith(("CLAUDECODE", "CLAUDE_CODE", "CODEX_", "OMP_")) and n not in {"CODEX_API_KEY", "CODEX_MODEL"}
    ]:
        environment.pop(name)
    if provider == "omp":
        environment["OMP_SKIP_SETUP"] = "1"  # a fresh HOME otherwise shows a setup wizard that swallows input
    environment[profiles.SCRIPTED_MODEL_BASE_URL_ENV] = endpoint
    profiles.configure_scripted_model_profile(provider, home, environment)
    if provider == "opencode":
        # Fail closed: only the scripted provider is enabled, so no built-in provider (OpenRouter and the like) can be chosen.
        config = home / ".config" / "opencode" / "opencode.json"
        value = json.loads(config.read_text())
        value["enabled_providers"] = [profiles.SCRIPTED_MODEL_PROVIDER]
        value["model"] = f"{profiles.SCRIPTED_MODEL_PROVIDER}/{profiles.SCRIPTED_MODEL_NAME}"
        config.write_text(json.dumps(value, indent=2))
    return environment


def screen_text(chunks: Iterable[TimedChunk]) -> str:
    replay = ScreenReplay([], rows=ROWS, columns=COLUMNS)
    for chunk in chunks:
        replay.feed(chunk.t, chunk.data)
    return "\n".join(replay.display)


@dataclass
class RunResult:
    provider: str
    mode: str
    status: str  # "measured" | "could_not_run"
    reason: str | None = None
    provider_version: str | None = None
    engine_version: str | None = None
    pace: dict[str, Any] | None = None
    stream_seconds: float | None = None
    markers: list[str] = field(default_factory=list)
    baseline: str | None = None
    terminal_first_seen_s: dict[str, float | None] = field(default_factory=dict)  # baseline first-seen, relative to prompt submit
    model_sent_s: dict[str, float | None] = field(default_factory=dict)  # when the model server wrote each marker, relative to submit
    diagnostics: dict[str, Any] = field(default_factory=dict)
    file_first_seen_s: dict[str, float | None] = field(default_factory=dict)  # provider wrote the marker to a file, relative to submit
    file_paths: list[str] = field(default_factory=list)
    file_scan_gap_s: float | None = None
    split: dict[str, Any] = field(default_factory=dict)
    pacing_error_s: float | None = None  # median |terminal spacing - scheduled spacing|
    lanes: list[LaneStats] = field(default_factory=list)
    lane_errors: dict[str, list[str]] = field(default_factory=dict)
    stream_sample: list[str] = field(default_factory=list)
    final_preview: dict[str, Any] = field(default_factory=dict)  # last live preview per session vs the scripted reply
    live_preview_check: dict[str, Any] = field(default_factory=dict)
    scratch: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in self.__dict__.items() if k not in {"lanes"}},
            "lanes": [lane.to_json() for lane in self.lanes],
        }


def _version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0] if out else None


def _wait_ready(
    session: ProviderPtySession, recording: Path, timing: Path, text: str, timeout: float, answers: Sequence[tuple[str, bytes]] = ()
) -> None:
    answered: set[str] = set()
    deadline = time.monotonic() + timeout
    last_size, still_since = -1, time.monotonic()
    while time.monotonic() < deadline:
        if not session.alive():
            raise RuntimeError(f"provider exited before it was ready ({session.process.returncode})\nlast output:\n{_tail_text(recording)}")
        for needle, keys in answers:
            with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
                if needle not in answered and needle in screen_text(load_timed_chunks(recording, timing)):
                    answered.add(needle)
                    session.write(keys)
        if not text:  # no known ready string: the TUI has drawn something and then gone quiet
            size = recording.stat().st_size if recording.exists() else 0
            if size != last_size:
                last_size, still_since = size, time.monotonic()
            elif size > 400 and time.monotonic() - still_since > 4:
                return
            time.sleep(0.5)
            continue
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
            if text in screen_text(load_timed_chunks(recording, timing)):
                time.sleep(1.5)  # let the TUI finish its first paint
                return
        time.sleep(0.5)
    last = ""
    with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
        last = screen_text(load_timed_chunks(recording, timing))
    raise RuntimeError(f"provider TUI never showed {text!r}; last screen:\n{last[-1200:]}")


def _tail_text(path: Path, limit: int = 1500) -> str:
    with contextlib.suppress(OSError):
        raw = path.read_bytes().decode("utf-8", errors="replace")
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", raw)[-limit:]
    return ""


def _collect_diagnostics(root: Path, model: ScriptedModel) -> dict[str, Any]:
    """Where live text could have stopped: model frames, engine log, Runtime Host log."""

    diag: dict[str, Any] = {"model_served_requests": model.served_requests()}
    host_log = root / "runtime-host.log"
    if host_log.exists():
        lines = host_log.read_text(errors="replace").splitlines()
        live = [line for line in lines if "live_transcript" in line]
        diag["host_log_live_transcript_lines"] = len(live)
        diag["host_log_live_transcript_sample"] = [x[:260] for x in live[:2] + live[-2:]]
        diag["host_log_runtime_events_posts"] = sum("/api/agents/runtime/events" in line for line in lines)
    engine_logs = sorted((root / "evidence").rglob("*.log")) if (root / "evidence").exists() else []
    engine_lines: list[str] = []
    for path in engine_logs:
        engine_lines += path.read_text(errors="replace").splitlines()
    keywords = re.compile(r"live_text|live preview|live_preview|runtime/events|runtime event|bridge.*delta|agent_message_delta", re.I)
    hits = [line for line in engine_lines if keywords.search(line)]
    diag["engine_log_lines"] = len(engine_lines)
    diag["engine_log_live_hits"] = len(hits)
    diag["engine_log_live_sample"] = [x[:260] for x in hits[:2] + hits[-2:]]
    return diag


def run_cell(
    provider: str,
    *,
    pace: Pacing,
    engine: Path,
    longhouse_cli: Path | None = None,
    keep: bool = False,
    grace_s: float = 90.0,
    mode: str = "shadow",
) -> RunResult:
    """One provider/mode cell.

    shadow: the provider command alone in a test-owned PTY; the Machine Agent ships its files, as for any Shadow session.
    helm: ``longhouse <provider>`` in that PTY (what a person types for a managed session).
    console: no PTY; a headless turn dispatched through the Runtime Host API. The reference clock is then the scripted
    model server's own send time for each frame, not a terminal.
    """

    from zerg.qa import live_session_toolkit as toolkit

    driver = PROVIDERS[provider]
    binary_path = shutil.which(driver.binary)
    result = RunResult(provider=provider, mode=mode, status="could_not_run")
    cli = longhouse_cli or (Path(shutil.which("longhouse")) if shutil.which("longhouse") else None)
    if mode == "helm" and cli is None:
        result.reason = "the native longhouse CLI is not installed on this machine"
        return result
    if binary_path is None:
        result.reason = f"{driver.binary} is not installed on this machine"
        return result
    result.provider_version = _version(binary_path)
    result.engine_version = _version(str(engine))
    root = _new_scratch()
    result.scratch = str(root)
    host = ScratchRuntimeHost(root)
    model = ScriptedModel(root, pace, "TTRUTH", "any_user" if provider == "antigravity" else "latest")
    shipper = None
    session: ProviderPtySession | None = None
    stop = threading.Event()
    recording, timing = root / "terminal.raw", root / "terminal.timing"
    try:
        model.start()
        home = root / "h"
        home.mkdir()
        workspace = root / "w"
        workspace.mkdir()
        environment = provider_environment(provider, model.endpoint, model.script, home)
        if provider == "antigravity":
            # An absolute path: an ambient wrapper that resolves "$HOME/.local/bin/agy" finds nothing under this HOME.
            environment["LONGHOUSE_ANTIGRAVITY_BIN"] = binary_path
        if provider == "codex":
            from zerg.qa.codex_auth import login_with_api_key

            key = environment.pop("CODEX_API_KEY")
            login_with_api_key(Path(binary_path), api_key=key, environment=environment, cwd=workspace, timeout=30)
        if provider == "claude":
            toolkit.prepare_claude_profile(
                binary=Path(binary_path), home=home, workspace=workspace, environment=environment, recording=root / "claude-onboarding.raw"
            )
            # Pre-answer the Bypass Permissions acceptance screen that the Helm launcher's flags trigger.
            settings = Path(environment["CLAUDE_CONFIG_DIR"]) / "settings.json"
            current = json.loads(settings.read_text()) if settings.exists() else {}
            current["skipDangerousModePermissionPrompt"] = True
            settings.write_text(json.dumps(current))
            # The engine advertises claude.turn_start (Console) only once the lifecycle hook is in settings.json; Helm
            # installs it at launch, Console does not, so install it the way `longhouse claude configure` does.
            subprocess.run(
                [str(cli), "claude", "configure", "--claude-dir", environment["CLAUDE_CONFIG_DIR"]],
                env=environment,
                check=True,
                capture_output=True,
                timeout=60,
            )
        if provider == "antigravity" and mode == "helm":
            _install_antigravity_hook(home, environment)
        if provider == "pi":
            # Console launches `pi` with no --model, so Pi's own default (an OpenRouter model) would be tried; pin the fake.
            agent_dir = home / ".pi" / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "settings.json").write_text(json.dumps({"defaultProvider": "longhouse-mock", "defaultModel": "mock-model"}))
        host.start()
        args = argparse.Namespace(api_url=host.url, agents_token=host.token, engine=engine, repo_root=REPO_ROOT)
        shipper = toolkit.start_transcript_shipper(provider, args, home=home, environment=environment, evidence_root=root / "evidence")
        api = RuntimeApi(host.url, host.token)

        if mode != "console":
            helm_argv = [
                str(cli),
                provider,
                "--cwd",
                str(workspace),
                "--url",
                host.url,
                *(["--token", host.token] if provider != "pi" else []),  # `longhouse pi` takes no --token (clap exit 2)
                f"--{provider}-bin",
                binary_path,
            ]
            if provider == "opencode":
                helm_argv += ["--model", "longhouse-mock/mock-model"]
            if provider == "pi":
                helm_argv += ["--provider", "longhouse-mock", "--model", "mock-model"]
            session = ProviderPtySession.start(
                argv=driver.argv(Path(binary_path), workspace) if mode == "shadow" else helm_argv,
                cwd=workspace,
                env=environment,
                terminal_path=recording,
                timing_path=timing,
                rows=ROWS,
                columns=COLUMNS,
            )
            _wait_ready(session, recording, timing, driver.ready_text, 120, driver.answers)

        text = paced_text(pace)
        schedule = pace_schedule(pace, text)
        markers = [word for _, word, _ in schedule]
        watchers: list[LaneWatcher | StreamWatcher] = [LaneWatcher(lane, markers, stop) for lane in api.poll_lanes()]
        watchers.append(StreamWatcher("workspace stream (SSE)", lambda: api.stream_lines(stop), markers, stop))
        if mode == "console":
            created = _create_console_session(api, provider, shipper.machine_name, workspace)
            console_session = str(created["session_id"])
            api.pinned_sessions = [console_session]  # the list endpoint may not show a headless session
        # Codex also writes streamed text to debug SQLite logs; only its rollout file is the transcript.
        files = FileWatcher(home, markers, stop, only_under=(".codex/sessions",) if provider == "codex" else ())
        files.scan_once()  # baseline of what already exists, so only writes after the prompt count
        for watcher in watchers:
            watcher.start()
        files.start()
        submitted = time.monotonic()
        if mode == "console":
            api.post(
                f"/api/agents/sessions/{console_session}/turns", {"message": PROMPT, "client_request_id": f"tt-{secrets.token_hex(4)}"}
            )
        else:
            assert session is not None
            session.submit_line(PROMPT)
        stream_seconds = pace.initial_delay + (len(text.split()) - 1) / pace.rate
        deadline = submitted + stream_seconds + grace_s + 30
        reference: dict[str, float | None] = {}
        polled_done_at: float | None = None

        def reference_times() -> dict[str, float | None]:
            if mode == "console":
                return model.sent_times(markers)
            return terminal_first_seen(load_timed_chunks(recording, timing), markers)

        while time.monotonic() < deadline:
            reference = reference_times()
            seen_ref = [m for m in markers if reference.get(m) is not None]
            all_seen = len(seen_ref) == len(markers)
            polled = [w for w in watchers if isinstance(w, LaneWatcher)]
            if all_seen and polled_done_at is None and all(m in w.first_seen for w in polled for m in seen_ref):
                polled_done_at = time.monotonic()
            if polled_done_at is not None and time.monotonic() > polled_done_at + 8:
                break
            if all_seen and time.monotonic() > max(reference[m] for m in markers) + grace_s:  # type: ignore[type-var]
                break
            if session is not None and not session.alive():
                break
            time.sleep(1.0)
        stop.set()
        for watcher in watchers:
            watcher.join(timeout=5)
        files.join(timeout=5)
        reference = reference_times()
        result.status = "measured"
        result.baseline = "model stream (frame written to the provider)" if mode == "console" else "terminal screen (pyte)"
        result.pace = {
            "tokens": pace.tokens,
            "rate_per_s": pace.rate,
            "initial_delay_s": pace.initial_delay,
            "marker_every": pace.marker_every,
        }
        result.stream_seconds = stream_seconds
        result.markers = markers
        result.terminal_first_seen_s = {m: (t - submitted if t is not None else None) for m, t in reference.items()}
        result.pacing_error_s = pacing_error(schedule, reference)
        if mode != "console":
            result.model_sent_s = {m: (t - submitted if t is not None else None) for m, t in model.sent_times(markers).items()}
        for watcher in watchers:
            stats = lane_stats(watcher.lane.name, reference, watcher.first_seen, watcher.resolution_s, markers, files.first_seen)
            stats.observations = len(watcher.response_times)
            result.lanes.append(stats)
            if watcher.errors:
                result.lane_errors[watcher.lane.name] = watcher.errors
            if isinstance(watcher, StreamWatcher):
                result.stream_sample = [f"events: {watcher.event_counts}", *watcher.sample]
                result.final_preview = {
                    "expected_len": len(text),
                    "sessions": {
                        sid: {"len": len(value), "equals_final_message": value.strip() == text.strip()}
                        for sid, value in watcher.final_preview.items()
                    },
                }
                result.live_preview_check = check_live_previews(watcher.previews, text, REASONING_WORD)
        result.file_first_seen_s = {m: (files.first_seen[m] - submitted if m in files.first_seen else None) for m in markers}
        result.file_paths = sorted(set(files.first_path.values()))
        result.file_scan_gap_s = files.resolution_s
        result.diagnostics = _collect_diagnostics(root, model)
        last = markers[-1]
        if reference.get(last) is not None:
            first_ref = next(reference[m] for m in markers if reference.get(m) is not None)
            result.split = {
                "stream_s": reference[last] - first_ref,
                "last_paint_s_after_submit": reference[last] - submitted,
                "provider_write_lag_after_last_paint_s": (files.first_seen[last] - reference[last]) if last in files.first_seen else None,
            }
        if result.diagnostics["model_served_requests"] < 1 or not any(t is not None for t in reference.values()):
            # Fail closed: a marker can only come from the scripted model, so no marker means the provider did not use it.
            result.status = "could_not_run"
            result.reason = "the provider never reached the scripted model (no marker, no served request)" + (
                "; last screen:\n" + screen_text(load_timed_chunks(recording, timing))[-1500:]
                if mode != "console" and recording.exists()
                else ""
            )
        return result
    except Exception as exc:  # noqa: BLE001 - becomes a "could not run" row with the cause
        result.status = "could_not_run"
        result.reason = f"{type(exc).__name__}: {exc}"
        if recording.exists() and "last screen" not in result.reason:
            result.reason += "\nlast output:\n" + _tail_text(recording)
        return result
    finally:
        stop.set()
        if session is not None:
            session.close()
        if shipper is not None:
            with contextlib.suppress(Exception):
                shipper.stop()
        host.stop()
        model.stop()
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
            result.scratch = None


def _install_antigravity_hook(home: Path, environment: dict[str, str]) -> None:
    """Put the Longhouse hook where `longhouse antigravity` insists on finding it, inside the isolated ``home`` only.

    Product finding: no command installs the Antigravity hook without also installing the machine service
    (`longhouse-server onboard` and `longhouse machine repair` both do), and the verifier may not import the
    subject's installer. So the harness writes the same files the installer writes: the shipped hook script (its
    source text is read from the subject's file, not imported, and fails loudly if it moves) and a global
    ``hooks.json`` entry, both under ``home``. The real script matters: on Stop it wakes the transcript shipper, which
    a no-op stub does not, and that changes the measured ship lag by seconds.
    """

    import ast

    from zerg.qa.provider_live_canary import _antigravity_hook_config

    if Path(environment["HOME"]).resolve() != home.resolve():
        raise RuntimeError("refusing to install the Antigravity hook outside the isolated HOME")
    source = (SERVER_ROOT / "zerg" / "services" / "antigravity_hook_inbox.py").read_text()
    template = next(
        (
            ast.literal_eval(node.value)
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_ANTIGRAVITY_HOOK_SCRIPT" for t in node.targets)
        ),
        None,
    )
    if not isinstance(template, str) or "__LONGHOUSE_HOME__" not in template:
        raise RuntimeError("the Antigravity hook script template moved; update _install_antigravity_hook")
    plugin = home / ".longhouse" / "managed-local" / "antigravity" / "plugins" / "longhouse-runtime"
    plugin.mkdir(parents=True)
    script = plugin / "longhouse-antigravity-hook.sh"
    script.write_text(template.replace("__LONGHOUSE_HOME__", shlex.quote(str(home / ".longhouse"))))
    script.chmod(0o755)
    (plugin / "plugin.json").write_text(json.dumps({"name": "longhouse-runtime"}) + "\n")
    hooks = home / ".gemini" / "config" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text(json.dumps({"longhouse-runtime": _antigravity_hook_config(script)}, indent=2) + "\n")


def _create_console_session(api: RuntimeApi, provider: str, device_id: str, cwd: Path) -> dict[str, Any]:
    payload = {
        "provider": provider,
        "device_id": device_id,
        "cwd": str(cwd),
        "project": "terminal-truth",
        "display_name": "terminal-truth",
        "launch_surface": "test",
    }
    deadline = time.monotonic() + 90
    while True:
        status, body = api.post_raw("/api/agents/sessions", payload)
        if status < 300:
            return json.loads(body)
        if status != 409 or "adapter_unavailable" not in body or time.monotonic() > deadline:
            raise RuntimeError(f"Console session create failed: HTTP {status} {body[:300]}")
        time.sleep(0.5)


def pacing_error(schedule: Sequence[tuple[int, str, float]], terminal: dict[str, float | None]) -> float | None:
    """Median |terminal spacing - scheduled spacing| from the first marker: did the terminal stream live?"""

    seen = [(due, terminal[word]) for _, word, due in schedule if terminal.get(word) is not None]
    if len(seen) < 2:
        return None
    base_due, base_t = seen[0]
    return statistics.median(abs((t - base_t) - (due - base_due)) for due, t in seen[1:])  # type: ignore[operator]


def _new_scratch() -> Path:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        path = SCRATCH_ROOT / secrets.token_hex(2)
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


# --------------------------------------------------------------------------------------
# Self-check: a TUI whose schedule we control, and lanes whose lateness we control
# --------------------------------------------------------------------------------------


def fake_tui(schedule: list[tuple[str, float]], truth: Path, *, tail: float = 0.3) -> None:
    """Print markers at ``offset`` seconds with in-place redraws, logging the monotonic time of each write.

    Output area scrolls normally; row 2 holds a spinner that is rewritten every 80 ms; the bottom
    row holds an input box that is rewritten every 200 ms and sometimes carries a marker that
    disappears from the screen again 150 ms later (text that was on screen but is gone from the
    final frame).
    """

    out = sys.stdout.buffer
    log = truth.open("a", buffering=1)
    rows = 40
    box_marker = ""
    box_until = 0.0
    frames = "|/-\\"
    ticks = 0

    def write(data: bytes) -> float:
        t = time.monotonic()
        out.write(data)
        out.flush()
        return t

    write(b"\x1b[2J\x1b[H")
    start = time.monotonic()
    log.write(json.dumps({"event": "start", "t": start}) + "\n")
    pending = sorted(schedule, key=lambda item: item[1])
    last_box = last_spin = 0.0
    end = (pending[-1][1] if pending else 0) + tail
    while time.monotonic() - start < end:
        now = time.monotonic()
        offset = now - start
        while pending and pending[0][1] <= offset:
            marker, _ = pending.pop(0)
            index = len(schedule) - len(pending) - 1
            if index % 3 == 2:  # every third marker lives only in the input box
                box_marker, box_until = marker, now + 0.15
                data = b"\x1b7\x1b[%d;1H\x1b[2K> typing %s\x1b8" % (rows, marker.encode())
                t = write(data)
                last_box = now
            else:
                t = write(f"reply {marker} streamed\r\n".encode())
            log.write(json.dumps({"event": "marker", "marker": marker, "t": t, "scheduled": schedule[index][1]}) + "\n")
        if now - last_spin >= 0.08:
            ticks += 1
            write(b"\x1b7\x1b[2;1H\x1b[2K%s thinking %d\x1b8" % (frames[ticks % 4].encode(), ticks))
            last_spin = now
        if now - last_box >= 0.2 or (box_marker and now > box_until):
            if box_marker and now > box_until:
                box_marker = ""
            write(b"\x1b7\x1b[%d;1H\x1b[2K> %s\x1b8" % (rows, box_marker.encode()))
            last_box = now
        time.sleep(0.002)
    log.write(json.dumps({"event": "end", "t": time.monotonic()}) + "\n")
    log.close()


def _read_truth(truth: Path) -> tuple[dict[str, float], float | None]:
    markers: dict[str, float] = {}
    end: float | None = None
    if truth.exists():
        for line in truth.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["event"] == "marker":
                markers[row["marker"]] = row["t"]
            elif row["event"] == "end":
                end = row["t"]
    return markers, end


@dataclass
class SelfCheck:
    schedule_error_max_s: float
    schedule_error_median_s: float
    chunk_resolution_note: str
    stream_seconds: float
    prompt_lane: LaneStats
    end_only_lane: LaneStats
    tolerance_s: float
    passed: bool
    detail: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "tolerance_s": self.tolerance_s,
            "schedule_error_max_s": self.schedule_error_max_s,
            "schedule_error_median_s": self.schedule_error_median_s,
            "stream_seconds": self.stream_seconds,
            "prompt_lane": self.prompt_lane.to_json(),
            "end_only_lane": self.end_only_lane.to_json(),
            "passed": self.passed,
            "detail": self.detail,
        }


def run_selfcheck(
    *, markers: int = 12, spacing_s: float = 0.5, serve_after_s: float = 0.3, tolerance_s: float = 0.05, poll_s: float = 0.05
) -> SelfCheck:
    """Fake TUI in the owned PTY plus two stub lanes; no provider, no Runtime Host, no network."""

    names = [f"SELFCHK{index:04d}" for index in range(markers)]
    schedule = [(name, 0.5 + index * spacing_s) for index, name in enumerate(names)]
    root = _new_scratch()
    truth, recording, timing = root / "truth.jsonl", root / "t.raw", root / "t.timing"
    (root / "schedule.json").write_text(json.dumps(schedule))
    stop = threading.Event()
    session: ProviderPtySession | None = None
    try:
        # Lane A serves each marker ``serve_after_s`` after the TUI wrote it. Lane B serves nothing until the TUI is done.
        def prompt_read() -> str | None:
            seen, _ = _read_truth(truth)
            now = time.monotonic()
            return " ".join(m for m, t in seen.items() if now >= t + serve_after_s)

        def end_only_read() -> str | None:
            seen, end = _read_truth(truth)
            return " ".join(seen) if end is not None else ""

        lanes = [Lane("prompt stub", prompt_read, poll_s), Lane("end-only stub", end_only_read, poll_s)]
        watchers = [LaneWatcher(lane, names, stop) for lane in lanes]
        for watcher in watchers:
            watcher.start()
        session = ProviderPtySession.start(
            argv=[
                sys.executable,
                "-m",
                "zerg.qa.terminal_truth",
                "fake-tui",
                "--schedule",
                str(root / "schedule.json"),
                "--truth",
                str(truth),
            ],
            cwd=SERVER_ROOT,
            env=dict(os.environ, PYTHONPATH=str(SERVER_ROOT)),
            terminal_path=recording,
            timing_path=timing,
            rows=ROWS,
            columns=COLUMNS,
        )
        deadline = time.monotonic() + schedule[-1][1] + 30
        while session.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(poll_s * 4)
        stop.set()
        for watcher in watchers:
            watcher.join(timeout=5)
        session.close()
        session = None
        terminal = terminal_first_seen(load_timed_chunks(recording, timing), names)
        truth_times, end = _read_truth(truth)
        errors = [terminal[n] - truth_times[n] for n in names if terminal.get(n) is not None and n in truth_times]  # type: ignore[operator]
        detail: list[str] = []
        missing = [n for n in names if terminal.get(n) is None]
        if missing:
            detail.append(f"markers never recovered from the screen: {missing}")
        if any(e < -0.001 for e in errors):
            detail.append("a marker was recovered from the screen before the fake TUI logged writing it (clock disagreement)")
        worst = max((abs(e) for e in errors), default=float("inf"))
        if worst > tolerance_s:
            detail.append(f"schedule recovered within {worst * 1000:.1f} ms, tolerance {tolerance_s * 1000:.0f} ms")
        stats = [lane_stats(w.lane.name, terminal, w.first_seen, w.resolution_s, names) for w in watchers]
        prompt, late = stats
        stream = (end or 0) - min(truth_times.values()) if truth_times else 0.0
        prompt_extra = [d - serve_after_s for d in prompt.delays.values() if d is not None]
        if len(prompt_extra) != len(names) or max(abs(x) for x in prompt_extra) > tolerance_s + 2 * poll_s + 0.05:
            detail.append(f"prompt stub (served {serve_after_s}s after write) not recovered: extra delay {prompt_extra}")
        # The end-only stub's delay for marker i must be (stream end - marker time) within poll resolution.
        for name, delay in late.delays.items():
            expected = (end or 0) - truth_times[name]
            if delay is None or abs(delay - expected) > tolerance_s + 3 * poll_s + 0.05:
                detail.append(f"end-only stub marker {name}: reported {delay}, truth {expected:.3f}")
        if late.first is None or late.first < 0.9 * stream:
            detail.append(f"end-only stub first-marker delay {late.first} is not about the stream length {stream:.2f}s")
        return SelfCheck(
            schedule_error_max_s=worst,
            schedule_error_median_s=statistics.median(abs(e) for e in errors) if errors else float("inf"),
            chunk_resolution_note="terminal side resolution is one PTY read",
            stream_seconds=stream,
            prompt_lane=prompt,
            end_only_lane=late,
            tolerance_s=tolerance_s,
            passed=not detail,
            detail=detail,
        )
    finally:
        stop.set()
        if session is not None:
            session.close()
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def check_live_previews(previews: dict[str, list[str]], expected: str, reasoning_word: str) -> dict[str, Any]:
    """What the live lane showed as reply text, against the reply the model sent.

    Judged on the session with the most previews. Each preview must continue the one before it (cumulative text:
    nothing repeated, reordered or replaced), the last must be the whole reply, and no reasoning word may appear.
    """
    if not previews:
        return {"previews": 0}
    session, texts = max(previews.items(), key=lambda item: len(item[1]))
    broken = next((i for i in range(1, len(texts)) if not texts[i].startswith(texts[i - 1])), None)
    return {
        "session": session,
        "previews": len(texts),
        "each_continues_the_last": broken is None,
        "first_break_at_preview": broken,
        "final_equals_reply": texts[-1] == expected.strip(),
        "final_length": len(texts[-1]),
        "expected_length": len(expected.strip()),
        "reasoning_leaked": any(reasoning_word in text for text in texts),
    }


def render_table(results: Sequence[RunResult]) -> str:
    lines = [
        "| provider | mode | lane | observations | resolution | first marker | median | worst | on screen, never served |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        if result.status != "measured":
            reason = (result.reason or "").splitlines()[0] if result.reason else ""
            lines.append(f"| {result.provider} | {result.mode} | could not run | | | | | | {reason} |")
            continue
        for lane in result.lanes:
            missing = f"{len(lane.never_served)} of {len(lane.delays)}" + (
                f" ({', '.join(lane.never_served[:4])}{'...' if len(lane.never_served) > 4 else ''})" if lane.never_served else ""
            )
            lines.append(
                f"| {result.provider} {result.provider_version or ''} | {result.mode} | {lane.lane} | {lane.observations} | "
                f"{'stream' if '(SSE)' in lane.lane else ('n/a' if lane.resolution_s is None else f'poll gap {lane.resolution_s * 1000:.0f} ms')} | "
                f"{_fmt(lane.first)} | {_fmt(lane.median)} | {_fmt(lane.worst)} | {missing} |"
            )
    return "\n".join(lines)


def render_final_preview(results: Sequence[RunResult]) -> str:
    """Whether the last live preview a web client received equals the scripted final message, per session."""

    lines = ["| provider | mode | final live preview equals the final message |", "|---|---|---|"]
    for result in results:
        if result.status != "measured":
            continue
        sessions = (result.final_preview or {}).get("sessions") or {}
        if not sessions:
            lines.append(f"| {result.provider} | {result.mode} | no live preview received |")
            continue
        parts = [
            f"{sid[:8]}: {'equal' if info['equals_final_message'] else 'DIFFERENT'} ({info['len']} of {result.final_preview['expected_len']} chars)"
            for sid, info in sessions.items()
        ]
        lines.append(f"| {result.provider} | {result.mode} | {'; '.join(parts)} |")
    return "\n".join(lines)


def render_split(results: Sequence[RunResult]) -> str:
    """Where each durable delay comes from: provider write lag after the last paint, then Longhouse's ship lag."""

    lines = [
        "| provider | mode | stream (first to last marker) | provider wrote last marker, after its last paint | lane | Longhouse ship lag first / median / worst (lane minus provider write) | file watch gap |",
        "|---|---|---|---|---|---|---|",
    ]
    for result in results:
        if result.status != "measured" or not result.split:
            continue
        gap = f"{result.file_scan_gap_s * 1000:.0f} ms" if result.file_scan_gap_s is not None else "-"
        for lane in result.lanes:
            if "SSE" in lane.lane:
                continue
            lines.append(
                f"| {result.provider} | {result.mode} | {_fmt(result.split['stream_s'])} | {_fmt(result.split['provider_write_lag_after_last_paint_s'])} | "
                f"{lane.lane} | {_fmt(lane.ship_first)} / {_fmt(lane.ship_median)} / {_fmt(lane.ship_worst)} | {gap} |"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="measure providers in shadow mode")
    run.add_argument("--provider", action="append", choices=sorted(PROVIDERS), help="repeatable; default all")
    run.add_argument("--tokens", type=int, default=400)
    run.add_argument("--rate", type=float, default=5.0)
    run.add_argument("--marker-every", type=int, default=20)
    run.add_argument("--initial-delay", type=float, default=1.0)
    run.add_argument(
        "--reasoning-tokens", type=int, default=0, help="stream this many reasoning words before the reply (chat completions providers)"
    )
    run.add_argument("--engine", type=Path, default=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or shutil.which("longhouse-engine") or ""))
    run.add_argument("--mode", action="append", choices=["shadow", "helm", "console"], help="repeatable; default shadow")
    run.add_argument("--longhouse-cli", type=Path, default=None, help="native longhouse CLI for helm mode (default: PATH)")
    run.add_argument("--keep", action="store_true")
    run.add_argument("--out", type=Path, help="write the JSON results here")
    check = sub.add_parser("selfcheck", help="fake TUI + stub lanes; exits non-zero if the harness cannot recover its own schedule")
    check.add_argument("--markers", type=int, default=12)
    check.add_argument("--spacing", type=float, default=0.5)
    check.add_argument("--out", type=Path)
    fake = sub.add_parser("fake-tui", help="internal: the fake TUI run inside the self-check PTY")
    fake.add_argument("--schedule", type=Path, required=True)
    fake.add_argument("--truth", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "fake-tui":
        fake_tui([(m, float(t)) for m, t in json.loads(args.schedule.read_text())], args.truth)
        return 0
    if args.command == "selfcheck":
        outcome = run_selfcheck(markers=args.markers, spacing_s=args.spacing)
        text = json.dumps(outcome.to_json(), indent=2)
        if args.out:
            args.out.write_text(text + "\n")
        print(text)
        return 0 if outcome.passed else 1
    pace = parse_pacing(
        {
            "rate": args.rate,
            "tokens": args.tokens,
            "marker_every": args.marker_every,
            "initial_delay": args.initial_delay,
            "marker_prefix": f"TT{os.getpid() % 10000:04d}",
            "reasoning_tokens": args.reasoning_tokens,
        }
    )
    if not args.engine.is_file():
        parser.error("no longhouse-engine binary: pass --engine")
    results = [
        run_cell(provider, pace=pace, engine=args.engine, longhouse_cli=args.longhouse_cli, keep=args.keep, mode=mode)
        for mode in (args.mode or ["shadow"])
        for provider in (args.provider or sorted(PROVIDERS))
    ]
    print(render_table(results))
    print()
    print(render_split(results))
    print()
    print(render_final_preview(results))
    for result in results:
        if result.live_preview_check:
            print(f"live preview check {result.provider} {result.mode}: {json.dumps(result.live_preview_check)}")
    if args.out:
        args.out.write_text(json.dumps([r.to_json() for r in results], indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
