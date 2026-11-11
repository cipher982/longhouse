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
import secrets
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
        }


def lane_stats(
    lane: str, terminal: dict[str, float | None], served: dict[str, float], resolution_s: float | None, order: Sequence[str]
) -> LaneStats:
    """Delay per marker that reached the terminal. Markers absent from the terminal are not scored."""

    delays: dict[str, float | None] = {}
    for marker in order:
        seen = terminal.get(marker)
        if seen is None:
            continue
        delays[marker] = served[marker] - seen if marker in served else None
    numbers = [d for d in delays.values() if d is not None]
    return LaneStats(
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
        self._stop = stop

    @property
    def resolution_s(self) -> float | None:
        gaps = [b - a for a, b in zip(self.response_times, self.response_times[1:], strict=False)]
        return max(gaps) if gaps else None

    def run(self) -> None:
        pending = set(self.markers)
        while pending and not self._stop.is_set():
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
                self._stop.wait(self.lane.interval)


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
        self._stop = stop

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
                for marker in [m for m in pending if m in line]:
                    self.first_seen[marker] = now
                    pending.discard(marker)
                if not pending or self._stop.is_set():
                    return
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
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
        self.session_id: str | None = None

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

    def find_session(self) -> str | None:
        if self.session_id is None:
            status, body = self.get("/api/agents/sessions?limit=5")
            if status == 200:
                sessions = json.loads(body).get("sessions") or []
                if sessions:
                    self.session_id = str(sessions[0]["id"])
        return self.session_id

    def _lane(self, name: str, path: str, *, browser: bool, interval: float) -> Lane:
        def read() -> str | None:
            session = self.find_session()
            if session is None:
                return None
            status, body = self.get(path.format(id=session), browser=browser)
            if status != 200:
                raise RuntimeError(f"{name}: HTTP {status} {body[:200]}")
            return body

        return Lane(name, read, interval)

    def poll_lanes(self) -> list[Lane]:
        return [
            self._lane("events (durable archive)", "/api/agents/sessions/{id}/events?limit=1000", browser=False, interval=0.05),
            self._lane("workspace (served to web)", "/api/timeline/sessions/{id}/workspace", browser=True, interval=0.1),
        ]

    def stream_lines(self, stop: threading.Event) -> Iterator[str]:
        """The workspace SSE (what the web client subscribes to), once the session exists."""

        while not stop.is_set():
            session = self.find_session()
            if session is None:
                stop.wait(0.05)
                continue
            connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
            try:
                headers = self._headers(True)
                headers["Accept"] = "text/event-stream"
                connection.request("GET", f"/api/timeline/sessions/{session}/workspace/stream", headers=headers)
                response = connection.getresponse()
                if response.status != 200:
                    raise RuntimeError(f"workspace stream HTTP {response.status}")
                while not stop.is_set():
                    raw = response.readline()
                    if not raw:
                        break
                    yield raw.decode("utf-8", errors="replace")
            finally:
                connection.close()
            stop.wait(0.2)


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

    def __init__(self, root: Path, pace: Pacing, match: str) -> None:
        self.root = root
        self.script = root / "scripted-model.json"
        self.script.write_text(
            json.dumps(
                {
                    "turns": [
                        {
                            "id": "paced",
                            "match": match,
                            "pace": {
                                "tokens": pace.tokens,
                                "rate": pace.rate,
                                "initial_delay": pace.initial_delay,
                                "marker_every": pace.marker_every,
                                "marker_prefix": pace.marker_prefix,
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


# --------------------------------------------------------------------------------------
# Providers: how each CLI is launched in a test-owned PTY
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDriver:
    provider: str
    binary: str
    ready_text: str  # on screen when the TUI accepts a prompt
    argv: Callable[[Path, Path], list[str]]  # (binary, workspace) -> argv


PROVIDERS: dict[str, ProviderDriver] = {
    "claude": ProviderDriver("claude", "claude", 'Try "', lambda b, w: [str(b)]),
    "codex": ProviderDriver(
        "codex",
        "codex",
        "Ask Codex",
        lambda b, w: [str(b), "--no-alt-screen", "--ask-for-approval", "never", "--sandbox", "read-only", "-C", str(w)],
    ),
    "opencode": ProviderDriver(
        "opencode",
        "opencode",
        "Ask anything",
        lambda b, w: [str(b), "--model", "longhouse-mock/mock-model", str(w), "--pure"],
    ),
    "pi": ProviderDriver("pi", "pi", "mock-model", lambda b, w: [str(b), "--provider", "longhouse-mock", "--model", "mock-model"]),
}


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
    terminal_first_seen_s: dict[str, float | None] = field(default_factory=dict)  # relative to prompt submit
    pacing_error_s: float | None = None  # median |terminal spacing - scheduled spacing|
    lanes: list[LaneStats] = field(default_factory=list)
    lane_errors: dict[str, list[str]] = field(default_factory=dict)
    stream_sample: list[str] = field(default_factory=list)
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


def _wait_ready(session: ProviderPtySession, recording: Path, timing: Path, text: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not session.alive():
            raise RuntimeError(f"provider exited before it was ready ({session.process.returncode})")
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
            if text in screen_text(load_timed_chunks(recording, timing)):
                time.sleep(1.5)  # let the TUI finish its first paint
                return
        time.sleep(0.5)
    last = ""
    with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
        last = screen_text(load_timed_chunks(recording, timing))
    raise RuntimeError(f"provider TUI never showed {text!r}; last screen:\n{last[-1200:]}")


def run_shadow(
    provider: str,
    *,
    pace: Pacing,
    engine: Path,
    keep: bool = False,
    grace_s: float = 90.0,
    mode: str = "shadow",
) -> RunResult:
    """Provider in a test-owned PTY (what a person sees); the Machine Agent ships its files, as for any Shadow session."""

    from zerg.qa import live_session_toolkit as toolkit

    driver = PROVIDERS[provider]
    binary_path = shutil.which(driver.binary)
    result = RunResult(provider=provider, mode=mode, status="could_not_run")
    longhouse_cli = shutil.which("longhouse")
    if mode == "helm" and longhouse_cli is None:
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
    model = ScriptedModel(root, pace, "TTRUTH")
    shipper = None
    session: ProviderPtySession | None = None
    stop = threading.Event()
    try:
        model.start()
        saved = {
            k: os.environ.get(k)
            for k in (scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV, scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV)
        }
        os.environ[scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV] = model.endpoint
        os.environ[scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV] = str(model.script)
        try:
            environment = scripted_model_profiles.scripted_model_environment(provider)
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
        home = root / "h"
        home.mkdir()
        workspace = root / "w"
        workspace.mkdir()
        scripted_model_profiles.configure_scripted_model_profile(provider, home, environment)
        if provider == "codex":
            from zerg.qa.codex_auth import login_with_api_key

            key = environment.pop("CODEX_API_KEY")
            login_with_api_key(Path(binary_path), api_key=key, environment=environment, cwd=workspace, timeout=30)
        if provider == "claude":
            toolkit.prepare_claude_profile(
                binary=Path(binary_path), home=home, workspace=workspace, environment=environment, recording=root / "claude-onboarding.raw"
            )
        host.start()
        args = argparse.Namespace(api_url=host.url, agents_token=host.token, engine=engine, repo_root=REPO_ROOT)
        shipper = toolkit.start_transcript_shipper(provider, args, home=home, environment=environment, evidence_root=root / "evidence")
        recording, timing = root / "terminal.raw", root / "terminal.timing"
        session = ProviderPtySession.start(
            argv=(
                driver.argv(Path(binary_path), workspace)
                if mode == "shadow"
                else [
                    str(longhouse_cli),
                    provider,
                    "--cwd",
                    str(workspace),
                    "--url",
                    host.url,
                    "--token",
                    host.token,
                    f"--{provider}-bin",
                    binary_path,
                ]
            ),
            cwd=workspace,
            env=environment,
            terminal_path=recording,
            timing_path=timing,
            rows=ROWS,
            columns=COLUMNS,
        )
        _wait_ready(session, recording, timing, driver.ready_text, 120)

        text = paced_text(pace)
        schedule = pace_schedule(pace, text)
        markers = [word for _, word, _ in schedule]
        api = RuntimeApi(host.url, host.token)
        watchers: list[LaneWatcher | StreamWatcher] = [LaneWatcher(lane, markers, stop) for lane in api.poll_lanes()]
        watchers.append(StreamWatcher("workspace stream (SSE)", lambda: api.stream_lines(stop), markers, stop))
        for watcher in watchers:
            watcher.start()
        submitted = time.monotonic()
        session.submit_line(PROMPT)
        stream_seconds = pace.initial_delay + (len(text.split()) - 1) / pace.rate
        deadline = submitted + stream_seconds + grace_s + 30
        terminal: dict[str, float | None] = {}
        polled_done_at: float | None = None
        while time.monotonic() < deadline:
            terminal = terminal_first_seen(load_timed_chunks(recording, timing), markers)
            on_screen = [m for m in markers if terminal.get(m) is not None]
            all_on_screen = len(on_screen) == len(markers)
            polled = [w for w in watchers if isinstance(w, LaneWatcher)]
            if all_on_screen and polled_done_at is None and all(m in w.first_seen for w in polled for m in on_screen):
                polled_done_at = time.monotonic()
            # Stop once the polled lanes have everything and the stream has had a few seconds more, or after the grace.
            if polled_done_at is not None and time.monotonic() > polled_done_at + 8:
                break
            if all_on_screen and time.monotonic() > max(terminal[m] for m in markers) + grace_s:  # type: ignore[type-var]
                break
            if not session.alive():
                break
            time.sleep(1.0)
        stop.set()
        for watcher in watchers:
            watcher.join(timeout=5)
        terminal = terminal_first_seen(load_timed_chunks(recording, timing), markers)
        result.status = "measured"
        result.pace = {
            "tokens": pace.tokens,
            "rate_per_s": pace.rate,
            "initial_delay_s": pace.initial_delay,
            "marker_every": pace.marker_every,
        }
        result.stream_seconds = stream_seconds
        result.markers = markers
        result.terminal_first_seen_s = {m: (t - submitted if t is not None else None) for m, t in terminal.items()}
        result.pacing_error_s = pacing_error(schedule, terminal)
        for watcher in watchers:
            stats = lane_stats(watcher.lane.name, terminal, watcher.first_seen, watcher.resolution_s, markers)
            stats.observations = len(watcher.response_times)
            result.lanes.append(stats)
            if watcher.errors:
                result.lane_errors[watcher.lane.name] = watcher.errors
            if isinstance(watcher, StreamWatcher):
                result.stream_sample = [f"events: {watcher.event_counts}", *watcher.sample]
        if not any(t is not None for t in terminal.values()):
            result.status = "could_not_run"
            result.reason = (
                "no marker ever reached the terminal screen; last screen:\n" + screen_text(load_timed_chunks(recording, timing))[-1500:]
            )
        return result
    except Exception as exc:  # noqa: BLE001 - becomes a "could not run" row with the cause
        result.status = "could_not_run"
        result.reason = f"{type(exc).__name__}: {exc}"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="measure providers in shadow mode")
    run.add_argument("--provider", action="append", choices=sorted(PROVIDERS), help="repeatable; default all")
    run.add_argument("--tokens", type=int, default=400)
    run.add_argument("--rate", type=float, default=5.0)
    run.add_argument("--marker-every", type=int, default=20)
    run.add_argument("--initial-delay", type=float, default=1.0)
    run.add_argument("--engine", type=Path, default=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or shutil.which("longhouse-engine") or ""))
    run.add_argument("--mode", action="append", choices=["shadow", "helm"], help="repeatable; default shadow")
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
        }
    )
    if not args.engine.is_file():
        parser.error("no longhouse-engine binary: pass --engine")
    results = [
        run_shadow(provider, pace=pace, engine=args.engine, keep=args.keep, mode=mode)
        for mode in (args.mode or ["shadow"])
        for provider in (args.provider or sorted(PROVIDERS))
    ]
    print(render_table(results))
    if args.out:
        args.out.write_text(json.dumps([r.to_json() for r in results], indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
