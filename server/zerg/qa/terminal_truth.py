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
import dataclasses
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
import zlib
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
        self.prompt = ""  # the cell's prompt; a preview equal to it is an echo of the prompt, counted and set aside
        self.prompt_echoes = 0
        self.tool_steps: list[tuple[str, str, str]] = []  # (session id, tool name, text) of each tool preview frame
        self._stop_event = stop

    def _record_preview(self, data: str) -> None:
        try:
            frame = json.loads(data)
        except ValueError:
            return
        preview = frame.get("transcript_preview")
        if isinstance(preview, dict) and (preview.get("role") == "user" or str(preview.get("text") or "").strip() == self.prompt):
            self.prompt_echoes += 1  # the person's own prompt shown as a preview (Pi Console does) is not reply text
            return
        if isinstance(preview, dict) and not preview.get("tool_name") and isinstance(preview.get("text"), str):
            self.previews.setdefault(str(frame.get("session_id")), []).append(preview["text"])
        elif isinstance(preview, dict) and preview.get("tool_name"):
            self.tool_steps.append((str(frame.get("session_id")), str(preview["tool_name"]), str(preview.get("text") or "")))

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
                        if (
                            isinstance(preview, dict)
                            and preview.get("role") != "user"
                            and str(preview.get("text") or "").strip() != self.prompt
                            and isinstance(preview.get("text"), str)
                        ):
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
    """The slice of the Runtime Host API the lanes read, scoped to ONE cell's sessions.

    A host shared by several cells holds every cell's sessions, and a lane that read "every session on the host" (the
    first design, with ``limit=20``) could score one cell's markers from another cell's text. A cell now reads only
    the sessions it created (``pinned_sessions``: a Console session by id) or that were launched in its own unique
    workspace directory (a Helm launch creates a managed session beside the file-discovered one, both in that cwd).
    """

    def __init__(self, api_url: str, token: str, *, workspace: Path | None = None) -> None:
        parsed = urlsplit(api_url)
        self.host, self.port = parsed.hostname or "127.0.0.1", parsed.port or 80
        self.token = token
        self.workspace = workspace.resolve() if workspace is not None else None
        self.pinned_sessions: list[str] = []
        self.known_sessions: set[str] = set()  # every id this cell's lanes ever read

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
        """This cell's sessions: the pinned ones plus every session whose cwd is this cell's workspace."""

        ids = list(self.pinned_sessions)
        if self.workspace is not None:
            status, body = self.get("/api/agents/sessions?limit=100&include_test=true")
            if status == 200:
                for item in json.loads(body).get("sessions") or []:
                    cwd = item.get("cwd")
                    if cwd and Path(str(cwd)).resolve() == self.workspace and str(item["id"]) not in ids:
                        ids.append(str(item["id"]))
        self.known_sessions.update(ids)
        return ids

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


class RequestScanner:
    """Follows the HTTP/1.1 requests in one client-to-host byte stream and hands each decoded body to ``note(path, body, t)``.

    The engine compresses request bodies (zstd or gzip), so searching the raw bytes finds nothing in exactly the large
    batches that carry the most text. This frames each request (Content-Length or chunked), decompresses the body and
    passes it on. A stream it cannot follow (an upgraded websocket, a framing it does not know) is scanned raw instead, so
    it never loses what the plain scan would have found.
    """

    HEAD_END = b"\r\n\r\n"

    def __init__(self, note: Callable[[bytes, bytes, float], None], on_request: Callable[[bytes], None]) -> None:
        self.note, self.on_request = note, on_request
        self.buffer = b""
        self.path = b""
        self.raw = False
        self._body_left: int | None = None
        self._chunked = False
        self._encoding = b""
        self._body = bytearray()

    def feed(self, data: bytes, now: float) -> None:
        if self.raw:
            self.note(self.path, data, now)
            return
        self.buffer += data
        try:
            while self._step(now):
                pass
        except Exception:  # noqa: BLE001 - lose the framing, keep scanning bytes
            self.raw = True
            self.note(self.path, self.buffer, now)
            self.buffer = b""

    def _step(self, now: float) -> bool:
        if self._body_left is None and not self._chunked:
            end = self.buffer.find(self.HEAD_END)
            if end < 0:
                return False
            head, self.buffer = self.buffer[:end], self.buffer[end + 4 :]
            lines = head.split(b"\r\n")
            method, target, _version = lines[0].split(b" ", 2)
            self.path = target
            self.on_request(target)
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(b":") for line in lines[1:])}
            if headers.get(b"upgrade", b"").lower() == b"websocket":
                self.raw = True
                return False
            self._encoding = headers.get(b"content-encoding", b"identity").lower()
            self._chunked = b"chunked" in headers.get(b"transfer-encoding", b"").lower()
            length = int(headers.get(b"content-length", b"0"))
            self._body = bytearray()
            if not self._chunked:
                self._body_left = length
                if length == 0:
                    self._body_left = None
                    return True
            return True
        if self._chunked:
            line_end = self.buffer.find(b"\r\n")
            if line_end < 0:
                return False
            size = int(self.buffer[:line_end].split(b";")[0], 16)
            need = line_end + 2 + size + 2
            if len(self.buffer) < need:
                return False
            self._body += self.buffer[line_end + 2 : line_end + 2 + size]
            self.buffer = self.buffer[need:]
            if size == 0:
                self._finish(now)
                self._chunked = False
            return True
        assert self._body_left is not None
        take = min(self._body_left, len(self.buffer))
        if take == 0:
            return False
        self._body += self.buffer[:take]
        self.buffer = self.buffer[take:]
        self._body_left -= take
        if self._body_left == 0:
            self._finish(now)
            self._body_left = None
            return True
        return False

    def _finish(self, now: float) -> None:
        body = bytes(self._body)
        try:
            if self._encoding in (b"gzip", b"x-gzip"):
                body = zlib.decompress(body, 31)
            elif self._encoding == b"deflate":
                body = zlib.decompress(body)
            elif self._encoding == b"zstd":
                import zstandard

                body = zstandard.ZstdDecompressor().decompressobj().decompress(body)
        except Exception:  # noqa: BLE001 - an undecodable body is scanned as it is
            pass
        self.note(self.path, body, now)


class TeeProxy(threading.Thread):
    """A loopback TCP pipe in front of the Runtime Host that notes which markers the engine POSTed as runtime events.

    The engine and the Helm launcher talk to the host through it. Bytes pass unchanged (HTTP, SSE and websockets alike);
    a copy of the client-to-host direction is framed into requests, decompressed and scanned for the cell markers, and each
    marker is attributed to the request path that carried it, so "the engine posted live text" is evidence from the wire and
    not a guess from a log. Only paths under ``/api/agents/runtime/`` count as live text: the durable transcript upload
    carries every marker too, later.
    """

    MARKER = re.compile(rb"TT[0-9a-f]{6}[0-9]{4}")

    def __init__(self, upstream_url: str) -> None:
        super().__init__(daemon=True, name="tee-proxy")
        parsed = urlsplit(upstream_url)
        self.upstream = (parsed.hostname or "127.0.0.1", parsed.port or 80)
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.runtime_posts: dict[str, float] = {}  # marker -> first time a runtime-events request carried it
        self.other_posts: dict[str, float] = {}  # marker -> first time any other request carried it
        self.runtime_post_count = 0
        self._lock = threading.Lock()
        self._closing = threading.Event()

    def run(self) -> None:
        while not self._closing.is_set():
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def close(self) -> None:
        self._closing.set()
        with contextlib.suppress(OSError):
            self.listener.close()

    def _serve(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self.upstream, timeout=10)
        except OSError:
            client.close()
            return
        upstream.settimeout(None)
        client.settimeout(None)
        threading.Thread(target=self._pipe, args=(upstream, client, False), daemon=True).start()
        self._pipe(client, upstream, True)

    def _on_request(self, path: bytes) -> None:
        if path.startswith(b"/api/agents/runtime/"):
            with self._lock:
                self.runtime_post_count += 1

    def _pipe(self, source: socket.socket, sink: socket.socket, scan: bool) -> None:
        scanner = RequestScanner(self._note, self._on_request) if scan else None
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                if scanner is not None:
                    scanner.feed(data, time.monotonic())
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (sink, source):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    sock.close()

    def _note(self, path: bytes, body: bytes, now: float) -> None:
        for found in self.MARKER.finditer(body):
            marker = found.group().decode()
            book = self.runtime_posts if path.startswith(b"/api/agents/runtime/") else self.other_posts
            with self._lock:
                book.setdefault(marker, now)


class ScratchRuntimeHost:
    """A loopback ``longhouse-server serve`` on its own database and HOME. Auth is off on loopback."""

    def __init__(self, root: Path, extra_environment: dict[str, str] | None = None) -> None:
        self.root = root
        self.extra_environment = dict(extra_environment or {})  # a mutation test shadows ``zerg`` with a patched copy here
        self.process: subprocess.Popen[bytes] | None = None
        self.url = ""
        self.token = ""

    def start(self) -> None:
        home = self.root / "host-home"
        home.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        environment = scripted_model_profiles.credential_free_environment(reject_ambient_credentials=False)
        environment.update(HOME=str(home), LLM_DISABLED="1", SKIP_DEMO_SEED="1", ENVIRONMENT="test:e2e")
        scrub_ambient_environment(environment)
        environment.update(self.extra_environment)
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


def pace_json(pace: Pacing) -> dict[str, Any]:
    return {
        "tokens": pace.tokens,
        "rate": pace.rate,
        "initial_delay": pace.initial_delay,
        "marker_every": pace.marker_every,
        "marker_prefix": pace.marker_prefix,
        "reasoning_tokens": pace.reasoning_tokens,
    }


class ScriptedModel:
    """The scripted model server as a child process, holding every turn of a pass (one per cell, matched by its nonce)."""

    def __init__(self, root: Path, turns: Sequence[dict[str, Any]]) -> None:
        self.root = root
        self.script = root / "scripted-model.json"
        self.script.write_text(json.dumps({"turns": list(turns)}))
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

    def served_requests(self, turn_ids: Sequence[str] | None = None) -> int:
        """Requests the model served, all of them or only those that matched one of ``turn_ids``."""

        with contextlib.suppress(OSError, ValueError, KeyError):
            receipt = json.loads((self.root / "model-receipt.json").read_text())
            if turn_ids is None:
                return int(receipt["scripted_model"]["served_requests"])
            return sum(1 for r in receipt["requests"] if r["status"] == "served" and r["matched_turn_id"] in turn_ids)
        return 0


# --------------------------------------------------------------------------------------
# Providers: how each CLI is launched in a test-owned PTY
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderDriver:
    provider: str
    binary: str
    ready_text: str  # on screen when the TUI accepts a prompt; never empty, so readiness is always checked
    argv: Callable[[Path, Path], list[str]]  # (binary, workspace) -> argv
    answers: tuple[tuple[str, bytes], ...] = ()  # (screen text, keys): startup prompts the harness answers once

    def __post_init__(self) -> None:
        if not self.ready_text.strip():
            raise ValueError(f"{self.provider}: a driver must name text that proves a ready prompt (an empty string checks nothing)")


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
    # Pi and OMP draw no prompt word. Their footer names the active model once the TUI has loaded it, which also proves
    # the scripted model was selected.
    "pi": ProviderDriver(
        "pi", "pi", "(longhouse-mock) mock-model", lambda b, w: [str(b), "--provider", "longhouse-mock", "--model", "mock-model"]
    ),
    "omp": ProviderDriver("omp", "omp", "Longhouse mock", lambda b, w: [str(b), "--model", "longhouse-mock/mock-model"]),
    "antigravity": ProviderDriver(
        "antigravity", "agy", "Gemini API key", lambda b, w: [str(b)], answers=(("Yes, I trust this folder", b"\r"),)
    ),
}


# What the person or agent running the harness has in their shell, and a provider must never inherit: Longhouse hook and
# session variables (a hook URL and token would send lifecycle events to a real Runtime Host under a real session id),
# the secret store, ssh agents, terminal and agent-harness markers. ``credential_free_environment`` keeps these when
# ``reject_ambient_credentials`` is off, so the harness removes them itself. The scripted-model variables are the harness's own.
_AMBIENT_PREFIXES = ("LONGHOUSE_", "INFISICAL_", "SSH_", "WARP_", "GCP_", "__DR_", "CLAUDE_", "AI_AGENT", "ZETA_", "AWS_")
_AMBIENT_KEEP = {
    "LONGHOUSE_SCRIPTED_MODEL_BASE_URL",
    "LONGHOUSE_SCRIPTED_MODEL_SCRIPT",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_PAGER",
}


def scrub_ambient_environment(environment: dict[str, str]) -> None:
    for name in [n for n in environment if n.startswith(_AMBIENT_PREFIXES) and n not in _AMBIENT_KEEP]:
        environment.pop(name)


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
    scrub_ambient_environment(environment)
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


# --------------------------------------------------------------------------------------
# Stages and the single verdict rule
# --------------------------------------------------------------------------------------

STAGES = ("preflight", "provider_emitted", "engine_posted", "host_published", "stream_delivered")
MEDIAN_DELAY_BUDGET_S = 0.75  # blocking; 4 x the worst median seen on 2026-11-11 (0.17 s), see terminal-truth-ci-gate.md
EARLY_FRACTION_MIN = 0.5  # blocking: this share of the markers must reach the client before the provider's last token
PAINT_LAG_BUDGET_S = (
    4.0  # Helm: median model-write to terminal-paint time; Claude Code measured 1.03 s (it paints about once a second), so 4x
)
LEDGER_PATH = SERVER_ROOT / "zerg" / "config" / "managed_provider_contracts.json"


@dataclass(frozen=True)
class Expectation:
    """What the provider ledger (``live_stream_signals`` in schemas/managed_providers.yml) says a cell should do."""

    live_reply_text: str  # expected | known_bad | not_measurable | informational
    source: str = ""
    reason: str = ""


def load_expectations(path: Path = LEDGER_PATH) -> dict[tuple[str, str], Expectation]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    found: dict[tuple[str, str], Expectation] = {}
    for item in payload["providers"]:
        for mode, cell in (item.get("live_stream_signals") or {}).items():
            found[(str(item["provider"]), mode)] = Expectation(cell["live_reply_text"], cell.get("source", ""), cell.get("reason", ""))
    return found


@dataclass(frozen=True)
class CellSpec:
    provider: str
    mode: str  # console | helm | shadow
    scenario: str = "long_reply"  # long_reply | two_message

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.mode}"


def declared_cells(expectations: dict[tuple[str, str], Expectation]) -> list[CellSpec]:
    """Every Helm and Console cell the ledger declares, in a stable order."""

    return [CellSpec(p, m) for (p, m) in sorted(expectations, key=lambda k: (k[1] != "console", k[0]))]


@dataclass
class StageResult:
    ok: bool | None  # None: not evaluated (an earlier stage made it meaningless)
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "evidence": self.evidence}


@dataclass
class Observation:
    """Everything the harness saw for one cell, on one monotonic clock, before any judgement."""

    mode: str
    markers: list[str]  # every scheduled marker word, in stream order
    messages: list[str]  # each assistant message the model sends, in order
    preflight_error: str | None = None
    ready_evidence: str = ""
    model_requests_served: int = 0
    reference: dict[str, float | None] = field(default_factory=dict)  # terminal paint (Helm) or model write (Console)
    model_sent: dict[str, float | None] = field(default_factory=dict)
    engine_posted: dict[str, float] = field(default_factory=dict)  # marker -> first runtime-events POST carrying it
    engine_other_posts: int = 0  # markers carried only by other requests (the durable upload)
    host_publishes: int = 0
    host_publish_max_len: int = 0
    stream_first_seen: dict[str, float] = field(default_factory=dict)  # workspace SSE
    previews: dict[str, list[str]] = field(default_factory=dict)
    tool_token: str | None = None  # two-message scenario: text only the tool step carries
    tool_on_client: bool = False
    reasoning_word: str = REASONING_WORD
    scenario: str = "long_reply"


def preview_segments(texts: Sequence[str]) -> list[list[str]]:
    """Split one session's cumulative previews where a preview does not continue the one before (a new message)."""

    segments: list[list[str]] = []
    for text in texts:
        if segments and text.startswith(segments[-1][-1]):
            segments[-1].append(text)
        else:
            segments.append([text])
    return segments


def check_two_message_previews(previews: dict[str, list[str]], messages: Sequence[str], reasoning_word: str) -> dict[str, Any]:
    """The live preview of a turn with two assistant messages: it shows the first, resets, then shows only the second."""

    if not previews:
        return {"previews": 0}
    session, texts = max(previews.items(), key=lambda item: len(item[1]))
    segments = preview_segments(texts)
    first, second = messages[0].strip(), messages[1].strip()
    return {
        "session": session,
        "previews": len(texts),
        "segments": len(segments),
        "first_message_complete": any(seg[-1] == first for seg in segments),
        "final_equals_second_message": texts[-1] == second,
        "second_starts_fresh": any(seg[0] != "" and not seg[0].startswith(first) and seg[-1] == second for seg in segments),
        "reasoning_leaked": any(reasoning_word in text for text in texts),
    }


def stream_checks(obs: Observation) -> list[tuple[str, bool, str]]:
    """The blocking checks of the stream_delivered stage: (name, ok, detail)."""

    checks: list[tuple[str, bool, str]] = []
    markers = obs.markers
    missing = [m for m in markers if m not in obs.stream_first_seen]
    checks.append(("every_marker_delivered", not missing, f"{len(markers) - len(missing)} of {len(markers)} markers on the live stream"))
    times = [obs.stream_first_seen[m] for m in markers if m in obs.stream_first_seen]
    in_order = all(a <= b for a, b in zip(times, times[1:], strict=False))
    checks.append(
        ("markers_in_order", in_order, "first-seen times nondecreasing along the reply" if in_order else "a later marker arrived first")
    )
    if obs.scenario == "two_message":
        two = check_two_message_previews(obs.previews, obs.messages, obs.reasoning_word)
        ok = (
            bool(two.get("previews"))
            and two.get("first_message_complete")
            and two.get("final_equals_second_message")
            and two.get("second_starts_fresh")
        )
        checks.append(("preview_resets_for_second_message", bool(ok), json.dumps(two)))
        checks.append(("no_reasoning_in_preview", not two.get("reasoning_leaked"), "reasoning words absent from the reply preview"))
        checks.append(("tool_step_on_client", obs.tool_on_client, f"tool token {obs.tool_token} on a client lane"))
    else:
        one = check_live_previews(obs.previews, obs.messages[-1], obs.reasoning_word)
        has = bool(one.get("previews"))
        checks.append(("final_preview_equals_message", has and bool(one.get("final_equals_reply")), json.dumps(one)))
        checks.append(
            (
                "each_preview_continues_the_last",
                has and bool(one.get("each_continues_the_last")),
                f"first break at {one.get('first_break_at_preview')}",
            )
        )
        checks.append(("no_reasoning_in_preview", has and not one.get("reasoning_leaked"), "reasoning words absent from the reply preview"))
    last = obs.reference.get(markers[-1]) if markers else None
    if last is not None:
        early = sum(1 for m in markers if m in obs.stream_first_seen and obs.stream_first_seen[m] < last)
        need = -(-len(markers) // 2)  # at least half, rounded up
        checks.append(
            ("half_delivered_before_last_token", early >= need, f"{early} of {len(markers)} before the provider's last token (need {need})")
        )
    delays = [obs.stream_first_seen[m] - t for m in markers if m in obs.stream_first_seen and (t := obs.reference.get(m)) is not None]
    if delays:
        median = statistics.median(delays)
        checks.append(
            ("median_delay_within_budget", median <= MEDIAN_DELAY_BUDGET_S, f"median {median:.3f} s, budget {MEDIAN_DELAY_BUDGET_S} s")
        )
    else:
        checks.append(("median_delay_within_budget", False, "no marker delivered to compare"))
    return checks


def judge(obs: Observation) -> dict[str, StageResult]:
    """The one verdict rule: five stages, each with evidence; the cell's verdict names the first that failed."""

    stages: dict[str, StageResult] = {}
    problem = obs.preflight_error
    if problem is None and not obs.ready_evidence.strip():
        problem = "no readiness evidence (the provider never proved a ready prompt)"
    if problem is None and obs.model_requests_served < 1:
        problem = "the fake model saw no request for this cell"
    stages["preflight"] = StageResult(
        problem is None,
        problem or "",
        {"ready_evidence": obs.ready_evidence[:300], "model_requests_served": obs.model_requests_served},
    )
    if problem is not None:
        for name in STAGES[1:]:
            stages[name] = StageResult(None, "not evaluated: preflight failed")
        return stages

    seen = [m for m in obs.markers if obs.reference.get(m) is not None]
    lags = [r - s for m in seen if (r := obs.reference[m]) is not None and (s := obs.model_sent.get(m)) is not None]
    emitted_problem = ""
    if not seen:
        emitted_problem = "zero markers observed"
    elif len(seen) < len(obs.markers):
        emitted_problem = f"{len(seen)} of {len(obs.markers)} markers observed"
    elif obs.mode == "helm" and lags and statistics.median(lags) > PAINT_LAG_BUDGET_S:
        emitted_problem = f"terminal painted markers {statistics.median(lags):.2f} s after the model wrote them"
    stages["provider_emitted"] = StageResult(
        not emitted_problem,
        emitted_problem,
        {"markers_observed": len(seen), "markers": len(obs.markers), "paint_lag_median_s": statistics.median(lags) if lags else None},
    )

    posted = [m for m in obs.markers if m in obs.engine_posted]
    posted_problem = "" if len(posted) == len(obs.markers) else f"engine posted live text for {len(posted)} of {len(obs.markers)} markers"
    stages["engine_posted"] = StageResult(
        not posted_problem,
        posted_problem,
        {"markers_in_runtime_posts": len(posted), "markers_only_in_other_posts": obs.engine_other_posts},
    )

    need = len(obs.messages[-1].strip()) if obs.messages else 0
    published_problem = ""
    if obs.host_publishes < 1:
        published_problem = "the host published no live preview for this cell's sessions"
    elif obs.host_publish_max_len < need:
        published_problem = f"the longest live preview the host published had {obs.host_publish_max_len} of {need} characters"
    stages["host_published"] = StageResult(
        not published_problem,
        published_problem,
        {"publishes": obs.host_publishes, "max_text_len": obs.host_publish_max_len, "final_message_len": need},
    )

    checks = stream_checks(obs)
    failed = [f"{name}: {detail}" for name, ok, detail in checks if not ok]
    stages["stream_delivered"] = StageResult(
        not failed,
        "; ".join(failed),
        {"checks": {name: {"ok": ok, "detail": detail} for name, ok, detail in checks}},
    )
    return stages


def first_failed_stage(stages: dict[str, StageResult]) -> str | None:
    return next((name for name in STAGES if stages.get(name) is not None and stages[name].ok is False), None)


def outcome_for(first_failed: str | None, expectation: Expectation, mode: str) -> str:
    """pass | fail | known_bad | known_bad_fixed | not_measurable | measured.

    A cell the ledger declares known-bad is reported as known-bad only when it stops where the defect is (the engine,
    the host or the stream). It failing earlier is a harness or provider problem, and it passing is a stale ledger: both
    fail the run, so a known-bad cell can neither hide a regression nor stay declared bad after it is fixed.
    """

    if mode == "shadow" or expectation.live_reply_text == "informational":
        return "measured"
    if expectation.live_reply_text == "not_measurable":
        return "not_measurable"
    if expectation.live_reply_text == "known_bad":
        if first_failed is None:
            return "known_bad_fixed"
        return "known_bad" if first_failed in ("engine_posted", "host_published", "stream_delivered") else "fail"
    return "pass" if first_failed is None else "fail"


def exit_code(results: Sequence[Any], declared: Sequence[CellSpec]) -> int:
    """0 only when every declared cell ran and ended pass or known_bad (or not_measurable); anything else is 1."""

    ran = {r.cell for r in results}
    if any(spec.key not in ran for spec in declared):
        return 1
    return 0 if all(r.outcome in {"pass", "known_bad", "not_measurable", "measured"} for r in results) else 1


@dataclass
class CellResult:
    provider: str
    mode: str
    status: str  # "measured" | "could_not_run" (kept for the table); the verdict is ``outcome``
    reason: str | None = None
    scenario: str = "long_reply"
    nonce: str = ""
    outcome: str = "fail"  # pass | fail | known_bad | known_bad_fixed | not_measurable | measured
    first_failed_stage: str | None = None
    stages: dict[str, Any] = field(default_factory=dict)
    expectation: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)  # provider and engine versions: recorded, never judged
    timing: dict[str, Any] = field(default_factory=dict)
    worst_marker: dict[str, Any] = field(default_factory=dict)  # recorded, never blocking
    pass_shape: str = "shared"  # shared | cold
    provider_version: str | None = None
    engine_version: str | None = None
    pace: dict[str, Any] | None = None
    stream_seconds: float | None = None
    markers: list[str] = field(default_factory=list)
    baseline: str | None = None
    terminal_first_seen_s: dict[str, float | None] = field(default_factory=dict)
    model_sent_s: dict[str, float | None] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    file_first_seen_s: dict[str, float | None] = field(default_factory=dict)
    file_paths: list[str] = field(default_factory=list)
    file_scan_gap_s: float | None = None
    split: dict[str, Any] = field(default_factory=dict)
    pacing_error_s: float | None = None
    lanes: list[LaneStats] = field(default_factory=list)
    lane_errors: dict[str, list[str]] = field(default_factory=dict)
    stream_sample: list[str] = field(default_factory=list)
    final_preview: dict[str, Any] = field(default_factory=dict)
    live_preview_check: dict[str, Any] = field(default_factory=dict)
    scratch: str | None = None

    @property
    def cell(self) -> str:
        return f"{self.provider}/{self.mode}"

    def to_json(self) -> dict[str, Any]:
        return {
            "cell": self.cell,
            **{k: v for k, v in self.__dict__.items() if k not in {"lanes"}},
            "lanes": [lane.to_json() for lane in self.lanes],
        }


RunResult = CellResult  # the name older callers and tests use


def _version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0] if out else None


def _wait_ready(
    session: ProviderPtySession, recording: Path, timing: Path, text: str, timeout: float, answers: Sequence[tuple[str, bytes]] = ()
) -> str:
    """Block until ``text`` is on the provider's screen, answering startup prompts. Returns the evidence: the screen around it."""

    if not text.strip():
        raise RuntimeError("no ready text: a driver must prove a ready prompt")
    answered: set[str] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not session.alive():
            raise RuntimeError(f"provider exited before it was ready ({session.process.returncode})\nlast output:\n{_tail_text(recording)}")
        for needle, keys in answers:
            with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
                if needle not in answered and needle in screen_text(load_timed_chunks(recording, timing)):
                    answered.add(needle)
                    session.write(keys)
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
            screen = screen_text(load_timed_chunks(recording, timing))
            if text in screen:
                time.sleep(1.5)  # let the TUI finish its first paint
                at = screen.index(text)
                return f"{text!r} on screen: ..." + screen[max(0, at - 60) : at + len(text) + 60]
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


_PUBLISH_LINE = re.compile(r"live_transcript publish session=(\S+) seq=\S+ age_ms=\S+ text_len=(\d+)")


def host_publishes(root: Path, sessions: Iterable[str]) -> tuple[int, int, int]:
    """(publish lines, longest text_len, runtime-events requests) the host logged for this cell's sessions."""

    wanted = set(sessions)
    log = root / "runtime-host.log"
    if not log.exists():
        return 0, 0, 0
    count, longest, requests = 0, 0, 0
    for line in log.read_text(errors="replace").splitlines():
        if "/api/agents/runtime/events" in line:
            requests += 1
        found = _PUBLISH_LINE.search(line)
        if found and found.group(1) in wanted:
            count += 1
            longest = max(longest, int(found.group(2)))
    return count, longest, requests


@dataclass
class CellPlan:
    """One cell of a pass: its nonce, the turns it adds to the shared model, its markers and prompt."""

    spec: CellSpec
    nonce: str
    pace: Pacing  # the long reply, or the first message of the two-message scenario
    pace_b: Pacing | None = None
    messages: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    tool_token: str | None = None

    @property
    def prompt(self) -> str:
        return f"TTRUTH{self.nonce} please stream the long reply"

    @property
    def turn_ids(self) -> list[str]:
        return [t["id"] for t in self.turns]


# Providers whose client misbehaves when the fake sends reasoning, so their cells run without it. Measured 2026-11-11:
# Claude Code 2.1.322 repeats the request (4 requests, the reply streamed 4 times) when the response carries a thinking
# block from the fake, signed or not. The cell result records that reasoning was off.
NO_REASONING_FAKE = {"claude"}


def plan_cell(spec: CellSpec, base: Pacing) -> CellPlan:
    if spec.provider in NO_REASONING_FAKE:
        base = dataclasses.replace(base, reasoning_tokens=0)
    nonce = secrets.token_hex(3)
    scope = "any_user" if spec.provider == "antigravity" else "latest"
    if spec.scenario == "two_message":
        nonce_b = secrets.token_hex(3)
        half = max(base.marker_every + 1, (base.tokens or 40) // 2)
        pace_a = dataclasses.replace(base, marker_prefix=f"TT{nonce}", tokens=half)
        pace_b = dataclasses.replace(base, marker_prefix=f"TT{nonce_b}", tokens=half, reasoning_tokens=0)
        tool_token = f"TTOOL{nonce}"
        text_a, text_b = paced_text(pace_a), paced_text(pace_b)
        turns = [
            {  # second request: the provider sends the tool result, whose output carries the tool token
                "id": f"{spec.key}:b",
                "match": tool_token,
                "match_scope": "latest",
                "reply": text_b,
                "pace": pace_json(pace_b),
            },
            {
                "id": f"{spec.key}:a",
                "match": f"TTRUTH{nonce}",
                "match_scope": scope,
                "reply": text_a,
                "tool_call": {"type": "shell", "command": f"echo {tool_token}"},
                "pace": pace_json(pace_a),
            },
        ]
        return CellPlan(spec, nonce, pace_a, pace_b, [text_a, text_b], turns, tool_token)
    pace = dataclasses.replace(base, marker_prefix=f"TT{nonce}")
    text = paced_text(pace)
    turn = {"id": spec.key, "match": f"TTRUTH{nonce}", "match_scope": scope, "pace": pace_json(pace)}
    return CellPlan(spec, nonce, pace, None, [text], [turn])


def plan_schedule(plan: CellPlan) -> list[tuple[int, str, float]]:
    """Marker (index, word, seconds after the stream opens). Message two's due times are unknown (a tool round trip comes first)."""

    first = pace_schedule(plan.pace, plan.messages[0])
    if plan.pace_b is None:
        return first
    return first + pace_schedule(plan.pace_b, plan.messages[1])


@dataclass
class Pass:
    """Infrastructure the cells of one pass share: Runtime Host, tee proxy, scripted model, Machine Agent and HOME."""

    root: Path
    host: ScratchRuntimeHost
    proxy: TeeProxy
    model: ScriptedModel
    shipper: Any
    home: Path
    environment: dict[str, str]
    cli: Path | None
    engine: Path
    timing: dict[str, float] = field(default_factory=dict)
    prepared: dict[str, float] = field(default_factory=dict)  # provider -> seconds to prepare its home
    binaries: dict[str, str] = field(default_factory=dict)


def _prepare_provider(
    provider: str, modes: set[str], *, home: Path, root: Path, cli: Path | None, binary: str, penv: dict[str, str], workspace_root: Path
) -> None:
    """Set one provider up inside the pass's HOME (config, login, hooks) before the Machine Agent starts."""

    from zerg.qa import live_session_toolkit as toolkit

    if provider == "codex":
        from zerg.qa.codex_auth import login_with_api_key

        key = penv.pop("CODEX_API_KEY")
        login_with_api_key(Path(binary), api_key=key, environment=penv, cwd=workspace_root, timeout=30)
    if provider == "claude":
        toolkit.prepare_claude_profile(
            binary=Path(binary), home=home, workspace=workspace_root, environment=penv, recording=root / "claude-onboarding.raw"
        )
        settings = Path(penv["CLAUDE_CONFIG_DIR"]) / "settings.json"
        current = json.loads(settings.read_text()) if settings.exists() else {}
        current["skipDangerousModePermissionPrompt"] = True  # the Bypass Permissions screen Helm's flags trigger
        settings.write_text(json.dumps(current))
        # Console advertises claude.turn_start only once the lifecycle hook is in settings.json (Helm installs it at launch).
        if cli is None:
            raise RuntimeError("the native longhouse CLI is needed to install the Claude lifecycle hook")
        subprocess.run(
            [str(cli), "claude", "configure", "--claude-dir", penv["CLAUDE_CONFIG_DIR"]],
            env=penv,
            check=True,
            capture_output=True,
            timeout=60,
        )
    if provider == "antigravity" and "helm" in modes:
        _install_antigravity_hook(home, penv)
    if provider == "pi":
        agent_dir = home / ".pi" / "agent"  # Console launches `pi` with no --model; pin the fake or Pi tries OpenRouter
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "settings.json").write_text(json.dumps({"defaultProvider": "longhouse-mock", "defaultModel": "mock-model"}))
    if provider == "omp":
        # The state that made the launcher pick a session directory OMP does not write: XDG_DATA_HOME unset and
        # ~/.local/share/omp present. Linux runners have it; the harness HOME must too, or the gate cannot see that defect.
        (home / ".local" / "share" / "omp").mkdir(parents=True, exist_ok=True)


def build_pass(
    specs: Sequence[CellSpec],
    plans: dict[str, CellPlan],
    *,
    engine: Path,
    cli: Path | None,
    host_environment: dict[str, str] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> Pass:
    from zerg.qa import live_session_toolkit as toolkit

    started = time.monotonic()
    root = _new_scratch()
    home = root / "h"
    home.mkdir()
    turns: list[dict[str, Any]] = []
    for spec in specs:
        turns += plans[spec.key].turns
    model = ScriptedModel(root, turns)
    host = ScratchRuntimeHost(root, host_environment)
    proxy: TeeProxy | None = None
    shipper = None
    try:
        model.start()
        providers = sorted({s.provider for s in specs})
        environment: dict[str, str] = {}
        per_provider: dict[str, dict[str, str]] = {}
        binaries: dict[str, str] = {}
        prepared: dict[str, float] = {}
        for provider in providers:
            t0 = time.monotonic()
            binary = shutil.which(PROVIDERS[provider].binary)
            if binary is None:
                raise RuntimeError(f"{PROVIDERS[provider].binary} is not installed on this machine")
            binaries[provider] = binary
            penv = provider_environment(provider, model.endpoint, model.script, home)
            if provider == "antigravity":
                # An absolute path: an ambient wrapper that resolves "$HOME/.local/bin/agy" finds nothing under this HOME.
                penv["LONGHOUSE_ANTIGRAVITY_BIN"] = binary
            workspace_root = root / "w" / provider
            workspace_root.mkdir(parents=True)
            modes = {s.mode for s in specs if s.provider == provider}
            _prepare_provider(provider, modes, home=home, root=root, cli=cli, binary=binary, penv=penv, workspace_root=workspace_root)
            per_provider[provider] = penv
            prepared[provider] = time.monotonic() - t0
        defaults = {"XDG_CONFIG_HOME": home / ".config", "PI_CODING_AGENT_DIR": home / ".pi" / "agent", "CODEX_HOME": home / ".codex"}
        for provider in providers:
            # A variable that only restates the provider's default location under HOME is dropped: exported to every
            # provider in the shared environment it moves the config roots of the others (OMP reads Pi's variable).
            for name, default in defaults.items():
                if per_provider[provider].get(name) == str(default):
                    per_provider[provider].pop(name)
        for provider in providers:  # one environment for the one Machine Agent (and the Helm launches): the union
            for key, value in per_provider[provider].items():
                if key in environment and environment[key] != value:
                    log(f"environment conflict on {key} between providers; keeping the first value")
                    continue
                environment[key] = value
        t0 = time.monotonic()
        host.start()
        host_s = time.monotonic() - t0
        proxy = TeeProxy(host.url)
        proxy.start()
        t0 = time.monotonic()
        args = argparse.Namespace(api_url=proxy.url, agents_token=host.token, engine=engine, repo_root=REPO_ROOT)
        shipper = toolkit.start_transcript_shipper(
            "claude" if "claude" in providers else providers[0], args, home=home, environment=environment, evidence_root=root / "evidence"
        )
        engine_s = time.monotonic() - t0
        timing = {"boot_host_s": host_s, "boot_engine_s": engine_s, "setup_s": time.monotonic() - started}
        return Pass(root, host, proxy, model, shipper, home, environment, cli, engine, timing, prepared, binaries)
    except BaseException:
        if shipper is not None:
            with contextlib.suppress(Exception):
                shipper.stop()
        if proxy is not None:
            proxy.close()
        host.stop()
        model.stop()
        shutil.rmtree(root, ignore_errors=True)
        raise


def stop_pass(pas: Pass, keep: bool) -> float:
    t0 = time.monotonic()
    with contextlib.suppress(Exception):
        pas.shipper.stop()
    pas.proxy.close()
    pas.host.stop()
    pas.model.stop()
    if not keep:
        shutil.rmtree(pas.root, ignore_errors=True)
    return time.monotonic() - t0


def run_cell_in(
    pas: Pass,
    plan: CellPlan,
    *,
    expectation: Expectation,
    base_pace: Pacing,
    grace_s: float,
    ready_timeout: float,
    drivers: dict[str, ProviderDriver],
) -> CellResult:
    """Run one cell inside a pass: its own workspace, session, nonce and observers; shared host, engine and model."""

    spec = plan.spec
    provider, mode = spec.provider, spec.mode
    driver = drivers[provider]
    binary = pas.binaries[provider]
    cell_started = time.monotonic()
    result = CellResult(provider=provider, mode=mode, status="could_not_run", scenario=spec.scenario, nonce=plan.nonce)
    result.expectation = {"live_reply_text": expectation.live_reply_text, "source": expectation.source, "reason": expectation.reason}
    result.provider_version = _version(binary)
    result.engine_version = _version(str(pas.engine))
    result.annotations = {
        "provider_version": result.provider_version,
        "engine_version": result.engine_version,
        "reasoning_from_fake": "off (client repeats the request)" if provider in NO_REASONING_FAKE else base_pace.reasoning_tokens,
    }
    result.timing = {"prepare_home_s": pas.prepared.get(provider), "prepare_home_note": "per provider, shared by its cells"}
    schedule = plan_schedule(plan)
    markers = [word for _, word, _ in schedule]
    obs = Observation(mode=mode, markers=markers, messages=plan.messages, scenario=spec.scenario, tool_token=plan.tool_token)
    workspace = pas.root / "w" / provider / f"{mode}-{plan.nonce}"
    workspace.mkdir(parents=True)
    api = RuntimeApi(pas.host.url, pas.host.token, workspace=workspace)
    stop = threading.Event()
    session: ProviderPtySession | None = None
    recording, timing_file = pas.root / f"terminal-{provider}-{mode}.raw", pas.root / f"terminal-{provider}-{mode}.timing"
    watchers: list[LaneWatcher | StreamWatcher] = []
    try:
        watch_for = markers + ([plan.tool_token] if plan.tool_token else [])
        if mode != "console":
            cli = pas.cli
            if cli is None:
                raise RuntimeError("the native longhouse CLI is not installed on this machine")
            helm_argv = [
                str(cli), provider, "--cwd", str(workspace), "--url", pas.proxy.url,
                *(["--token", pas.host.token] if provider != "pi" else []),  # `longhouse pi` takes no --token (clap exit 2)
                f"--{provider}-bin", binary,
            ]  # fmt: skip
            if provider == "opencode":
                helm_argv += ["--model", "longhouse-mock/mock-model"]
            if provider == "pi":
                helm_argv += ["--provider", "longhouse-mock", "--model", "mock-model"]
            t0 = time.monotonic()
            session = ProviderPtySession.start(
                argv=driver.argv(Path(binary), workspace) if mode == "shadow" else helm_argv,
                cwd=workspace,
                env=pas.environment,
                terminal_path=recording,
                timing_path=timing_file,
                rows=ROWS,
                columns=COLUMNS,
            )
            obs.ready_evidence = _wait_ready(session, recording, timing_file, driver.ready_text, ready_timeout, driver.answers)
            result.timing["provider_start_to_ready_s"] = time.monotonic() - t0

        watchers = [LaneWatcher(lane, watch_for, stop) for lane in api.poll_lanes()]
        stream = StreamWatcher("workspace stream (SSE)", lambda: api.stream_lines(stop), watch_for, stop)
        stream.prompt = plan.prompt
        watchers.append(stream)
        console_session = ""
        if mode == "console":
            t0 = time.monotonic()
            created = _create_console_session(api, provider, pas.shipper.machine_name, workspace)
            console_session = str(created["session_id"])
            api.pinned_sessions = [console_session]  # the list endpoint may not show a headless session
            obs.ready_evidence = f"console session {console_session} created on machine {pas.shipper.machine_name}; adapter available"
            result.timing["provider_start_to_ready_s"] = time.monotonic() - t0
        # Codex also writes streamed text to debug SQLite logs; only its rollout file is the transcript.
        files = FileWatcher(pas.home, markers, stop, only_under=(".codex/sessions",) if provider == "codex" else ())
        files.scan_once()  # baseline of what already exists, so only writes after the prompt count
        for watcher in watchers:
            watcher.start()
        files.start()
        submitted = time.monotonic()
        if mode == "console":
            api.post(
                f"/api/agents/sessions/{console_session}/turns", {"message": plan.prompt, "client_request_id": f"tt-{secrets.token_hex(4)}"}
            )
        else:
            assert session is not None
            session.submit_line(plan.prompt)
        stream_seconds = base_pace.initial_delay + (len(plan.messages[0].split()) - 1) / base_pace.rate
        if plan.pace_b is not None:
            stream_seconds += 10 + base_pace.initial_delay + (len(plan.messages[1].split()) - 1) / base_pace.rate
        deadline = submitted + stream_seconds + grace_s + 30
        reference: dict[str, float | None] = {}
        polled_done_at: float | None = None

        def reference_times() -> dict[str, float | None]:
            if mode == "console":
                return pas.model.sent_times(markers)
            return terminal_first_seen(load_timed_chunks(recording, timing_file), markers)

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
        settle_from = time.monotonic()
        stop.set()
        for watcher in watchers:
            watcher.join(timeout=5)
        files.join(timeout=5)
        reference = reference_times()
        last_ref = max((t for t in reference.values() if t is not None), default=None)
        result.timing["stream_s"] = (last_ref - submitted) if last_ref is not None else None
        result.timing["settle_s"] = time.monotonic() - settle_from
        result.status = "measured"
        result.baseline = "model stream (frame written to the provider)" if mode == "console" else "terminal screen (pyte)"
        result.pace = {
            "tokens": base_pace.tokens,
            "rate_per_s": base_pace.rate,
            "initial_delay_s": base_pace.initial_delay,
            "marker_every": base_pace.marker_every,
            "reasoning_tokens": plan.pace.reasoning_tokens,
        }
        result.stream_seconds = stream_seconds
        result.markers = markers
        sent = pas.model.sent_times(markers)
        result.terminal_first_seen_s = {m: (t - submitted if t is not None else None) for m, t in reference.items()}
        result.pacing_error_s = pacing_error(schedule, reference) if plan.pace_b is None else None
        if mode != "console":
            result.model_sent_s = {m: (t - submitted if t is not None else None) for m, t in sent.items()}
        text = plan.messages[-1]
        for watcher in watchers:
            stats = lane_stats(watcher.lane.name, reference, watcher.first_seen, watcher.resolution_s, markers, files.first_seen)
            stats.observations = len(watcher.response_times)
            result.lanes.append(stats)
            if watcher.errors:
                result.lane_errors[watcher.lane.name] = watcher.errors
            if isinstance(watcher, StreamWatcher):
                result.stream_sample = [
                    f"events: {watcher.event_counts}",
                    f"prompt echoes set aside: {watcher.prompt_echoes}",
                    *watcher.sample,
                ]
                result.final_preview = {
                    "expected_len": len(text),
                    "sessions": {
                        sid: {"len": len(v), "equals_final_message": v.strip() == text.strip()} for sid, v in watcher.final_preview.items()
                    },
                }
                result.live_preview_check = (
                    check_two_message_previews(watcher.previews, plan.messages, REASONING_WORD)
                    if plan.pace_b is not None
                    else check_live_previews(watcher.previews, text, REASONING_WORD)
                )
                obs.stream_first_seen = dict(watcher.first_seen)
                obs.previews = watcher.previews
                obs.tool_on_client = bool(plan.tool_token and (plan.tool_token in watcher.first_seen or watcher.tool_steps))
        if plan.tool_token:
            workspace_lane = [w for w in watchers if isinstance(w, LaneWatcher) and "workspace" in w.lane.name]
            obs.tool_on_client = obs.tool_on_client or any(plan.tool_token in w.first_seen for w in workspace_lane)
        result.file_first_seen_s = {m: (files.first_seen[m] - submitted if m in files.first_seen else None) for m in markers}
        result.file_paths = sorted(set(files.first_path.values()))
        result.file_scan_gap_s = files.resolution_s
        publishes, longest, runtime_requests = host_publishes(pas.root, api.known_sessions)
        obs.reference, obs.model_sent = reference, sent
        obs.host_publishes, obs.host_publish_max_len = publishes, longest
        wanted = set(markers)
        obs.engine_posted = {m: t for m, t in pas.proxy.runtime_posts.items() if m in wanted}
        obs.engine_other_posts = sum(1 for m in markers if m in pas.proxy.other_posts and m not in pas.proxy.runtime_posts)
        result.diagnostics = {
            "cell_sessions": sorted(api.known_sessions),
            "host_publishes": publishes,
            "host_publish_max_text_len": longest,
            "host_runtime_events_requests_pass_total": runtime_requests,
            "tee_runtime_posts_pass_total": pas.proxy.runtime_post_count,
        }
        last = markers[-1]
        if reference.get(last) is not None:
            first_ref = next(reference[m] for m in markers if reference.get(m) is not None)
            result.split = {
                "stream_s": reference[last] - first_ref,
                "last_paint_s_after_submit": reference[last] - submitted,
                "provider_write_lag_after_last_paint_s": (files.first_seen[last] - reference[last]) if last in files.first_seen else None,
            }
    except Exception as exc:  # noqa: BLE001 - becomes a preflight failure carrying the cause
        obs.preflight_error = f"{type(exc).__name__}: {exc}"
        result.reason = obs.preflight_error
        if recording.exists() and "last screen" not in obs.preflight_error:
            result.reason += "\nlast output:\n" + _tail_text(recording)
    finally:
        stop.set()
        t0 = time.monotonic()
        if session is not None:
            session.close()
        result.timing["teardown_s"] = time.monotonic() - t0
    obs.model_requests_served = pas.model.served_requests(plan.turn_ids)
    result.diagnostics["model_served_requests_for_cell"] = obs.model_requests_served
    stages = judge(obs)
    result.stages = {name: stage.to_json() for name, stage in stages.items()}
    result.first_failed_stage = first_failed_stage(stages)
    result.outcome = outcome_for(result.first_failed_stage, expectation, mode)
    if result.first_failed_stage is not None and not result.reason:
        result.reason = f"{result.first_failed_stage}: {stages[result.first_failed_stage].reason}"
    sse = next((lane for lane in result.lanes if "SSE" in lane.lane), None)
    worst = max(((d, m) for m, d in (sse.delays.items() if sse else []) if d is not None), default=None)
    result.worst_marker = {"marker": worst[1], "delay_s": worst[0]} if worst else {}
    result.timing["cell_wall_s"] = time.monotonic() - cell_started
    result.timing.update({f"pass_{k}": v for k, v in pas.timing.items()})
    return result


HOME_EXCLUSIVE = {"omp"}


def run_suite(
    specs: Sequence[CellSpec],
    *,
    pace: Pacing,
    engine: Path,
    longhouse_cli: Path | None = None,
    keep: bool = False,
    grace_s: float = 90.0,
    cold: str | None = "first",
    expectations: dict[tuple[str, str], Expectation] | None = None,
    ready_timeout: float = 120.0,
    drivers: dict[str, ProviderDriver] | None = None,
    host_environment: dict[str, str] | None = None,
    log: Callable[[str], None] = lambda _m: None,
) -> list[CellResult]:
    """Run ``specs``: the cold cell alone in a pass of its own (own host, engine and HOME), the rest in one shared pass.

    The cold pass keeps the one-cell-one-host shape on every run, so a defect that appears only when cells share a host
    (or only when they do not) shows as a difference between the two.
    """

    expectations = expectations if expectations is not None else load_expectations()
    drivers = drivers or PROVIDERS
    cli = longhouse_cli or (Path(shutil.which("longhouse")) if shutil.which("longhouse") else None)

    def expectation_of(spec: CellSpec) -> Expectation:
        default = Expectation("informational" if spec.mode == "shadow" else "expected")
        return Expectation("informational") if spec.mode == "shadow" else expectations.get((spec.provider, spec.mode), default)

    def blank(spec: CellSpec, expectation: Expectation) -> dict[str, Any]:
        return {"live_reply_text": expectation.live_reply_text, "source": expectation.source, "reason": expectation.reason}

    runnable: list[CellSpec] = []
    results: list[CellResult] = []
    for spec in specs:
        expectation = expectation_of(spec)
        if expectation.live_reply_text == "not_measurable":
            results.append(
                CellResult(
                    spec.provider, spec.mode, "could_not_run", expectation.reason, scenario=spec.scenario, outcome="not_measurable",
                    expectation=blank(spec, expectation),
                )
            )  # fmt: skip
        else:
            runnable.append(spec)
    cold_spec: CellSpec | None = None
    if cold and runnable:
        cold_spec = runnable[0] if cold == "first" else next((s for s in runnable if s.key == cold), None)
    groups: list[tuple[list[CellSpec], str]] = []
    if cold_spec is not None:
        groups.append(([cold_spec], "cold"))
    rest = [s for s in runnable if s is not cold_spec]
    # OMP and Pi share a config layout, so in one HOME each breaks the other's launch (measured: OMP never selects the
    # scripted model, Pi's previews interleave). OMP's shared cells get a pass of their own.
    for apart in (False, True):
        part = [s for s in rest if (s.provider in HOME_EXCLUSIVE) == apart]
        if part:
            groups.append((part, "shared"))
    for group, shape in groups:
        plans = {s.key: plan_cell(s, pace) for s in group}
        try:
            pas = build_pass(group, plans, engine=engine, cli=cli, host_environment=host_environment, log=log)
        except Exception as exc:  # noqa: BLE001
            for spec in group:
                reason = f"pass setup failed: {type(exc).__name__}: {exc}"
                results.append(
                    CellResult(
                        spec.provider, spec.mode, "could_not_run", reason, scenario=spec.scenario, pass_shape=shape, outcome="fail",
                        first_failed_stage="preflight", stages={"preflight": StageResult(False, reason).to_json()},
                        expectation=blank(spec, expectation_of(spec)),
                    )
                )  # fmt: skip
            continue
        try:
            for spec in group:
                log(f"running {spec.key} ({shape} pass)")
                cell = run_cell_in(
                    pas,
                    plans[spec.key],
                    expectation=expectation_of(spec),
                    base_pace=pace,
                    grace_s=grace_s,
                    ready_timeout=ready_timeout,
                    drivers=drivers,
                )
                cell.pass_shape = shape
                cell.scratch = str(pas.root) if keep else None
                results.append(cell)
        finally:
            torn = stop_pass(pas, keep)
        for cell in results:
            if cell.pass_shape == shape and "pass_boot_host_s" in cell.timing:
                cell.timing["pass_teardown_s"] = torn
    return results


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
        "break_context": [texts[broken - 1][-80:], texts[broken][:80]] if broken else None,
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


def render_verdicts(results: Sequence[CellResult]) -> str:
    """One row per cell: the verdict and the first stage where the text stopped."""

    lines = [
        "| cell | verdict | first failed stage | provider | SSE median | worst marker (not blocking) | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        sse = next((lane for lane in r.lanes if "SSE" in lane.lane), None)
        reason = (r.reason or "").splitlines()[0][:160] if r.reason else ""
        lines.append(
            f"| {r.cell} ({r.scenario}) | {r.outcome} | {r.first_failed_stage or '-'} | {r.provider_version or '-'} | "
            f"{_fmt(sse.median if sse else None)} | {_fmt(r.worst_marker.get('delay_s'))} | {reason} |"
        )
    return "\n".join(lines)


def render_timing(results: Sequence[CellResult]) -> str:
    """Where a cell's wall time goes. Boot and teardown of host and engine are a pass's, shared by its cells (shared pass)."""

    keys = [
        ("prepare_home_s", "prepare home"),
        ("pass_boot_host_s", "boot host"),
        ("pass_boot_engine_s", "boot engine"),
        ("provider_start_to_ready_s", "provider start to ready"),
        ("stream_s", "stream"),
        ("settle_s", "settle"),
        ("teardown_s", "teardown"),
        ("cell_wall_s", "cell total"),
    ]
    lines = ["| cell | pass | " + " | ".join(label for _, label in keys) + " |", "|---|---|" + "---|" * len(keys)]
    for r in results:
        if not r.timing:
            continue
        lines.append(f"| {r.cell} | {r.pass_shape} | " + " | ".join(_fmt(r.timing.get(k)) for k, _ in keys) + " |")
    return "\n".join(lines)


def summary_json(results: Sequence[CellResult], declared: Sequence[CellSpec], code: int) -> dict[str, Any]:
    ran = {r.cell for r in results}
    return {
        "exit_code": code,
        "declared": [s.key for s in declared],
        "missing": [s.key for s in declared if s.key not in ran],
        "cells": {
            r.cell: {
                "outcome": r.outcome,
                "first_failed_stage": r.first_failed_stage,
                "reason": (r.reason or "")[:400],
                "provider_version": r.provider_version,
                "pass_shape": r.pass_shape,
            }
            for r in results
        },
    }


def write_results(results: Sequence[CellResult], declared: Sequence[CellSpec], code: int, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in results:
        (out_dir / f"{r.provider}-{r.mode}-{r.scenario}.json").write_text(json.dumps(r.to_json(), indent=2) + "\n")
    (out_dir / "summary.json").write_text(json.dumps(summary_json(results, declared, code), indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="judge cells; exits 1 on any fail, any declared cell that did not run, or a stale known-bad declaration",
    )
    run.add_argument(
        "--provider", action="append", choices=sorted({*PROVIDERS, "cursor"}), help="repeatable; default every declared provider"
    )
    run.add_argument("--tokens", type=int, default=100)
    run.add_argument("--rate", type=float, default=20.0)
    run.add_argument("--marker-every", type=int, default=20)
    run.add_argument("--initial-delay", type=float, default=1.0)
    run.add_argument(
        "--reasoning-tokens", type=int, default=5, help="reasoning words streamed before the reply, on every protocol (0 turns it off)"
    )
    run.add_argument("--scenario", choices=["long_reply", "two_message"], default="long_reply")
    run.add_argument("--engine", type=Path, default=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or shutil.which("longhouse-engine") or ""))
    run.add_argument("--mode", action="append", choices=["shadow", "helm", "console"], help="repeatable; default console and helm")
    run.add_argument("--longhouse-cli", type=Path, default=None, help="native longhouse CLI for helm mode (default: PATH)")
    run.add_argument(
        "--cold-cell", default="first", help="the cell run alone in its own host, engine and HOME: 'first', 'none' or provider/mode"
    )
    run.add_argument("--ready-timeout", type=float, default=120.0)
    run.add_argument("--keep", action="store_true")
    run.add_argument("--out", type=Path, help="write all cell results here as one JSON file")
    run.add_argument("--out-dir", type=Path, help="write one JSON per cell and summary.json here")
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
            "marker_prefix": "TT",
            "reasoning_tokens": args.reasoning_tokens,
        }
    )
    if not args.engine.is_file():
        parser.error("no longhouse-engine binary: pass --engine")
    expectations = load_expectations()
    modes = args.mode or ["console", "helm"]
    providers = args.provider or sorted({p for p, _ in expectations})
    declared = [
        CellSpec(provider, mode, args.scenario)
        for mode in modes
        for provider in providers
        if mode == "shadow" or (provider, mode) in expectations
    ]
    results = run_suite(
        declared,
        pace=pace,
        engine=args.engine,
        longhouse_cli=args.longhouse_cli,
        keep=args.keep,
        cold=None if args.cold_cell == "none" else args.cold_cell,
        expectations=expectations,
        ready_timeout=args.ready_timeout,
        log=lambda message: print(message, file=sys.stderr, flush=True),
    )
    code = exit_code(results, declared)
    print(render_verdicts(results))
    print()
    print(render_timing(results))
    print()
    print(render_table([r for r in results if r.status == "measured"]))
    print()
    print(render_split(results))
    print()
    print(render_final_preview(results))
    for result in results:
        if result.live_preview_check:
            print(f"live preview check {result.cell}: {json.dumps(result.live_preview_check)}")
    if args.out:
        args.out.write_text(json.dumps([r.to_json() for r in results], indent=2) + "\n")
    if args.out_dir:
        write_results(results, declared, code, args.out_dir)
    print(f"\nterminal truth: {'PASS' if code == 0 else 'FAIL'} (exit {code})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
