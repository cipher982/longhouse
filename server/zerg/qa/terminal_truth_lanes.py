"""Lane observers: polled and streamed Runtime Host surfaces, the provider's files, and the wire between engine and host."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import queue
import re
import socket
import statistics
import threading
import time
import zlib
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit


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


class Spans:
    """Where a cell's wall time went. ``mark(name)`` gives the time since the last mark to ``name``; the spans add up to the cell.

    Kept in every result row so that a run says what its cells waited on. 2026-11-13: a Console reply cell was 17 s of which
    the reply was 7; the other 10 were found by reading the code, because no run had recorded them.
    """

    def __init__(self) -> None:
        self.started = self._last = time.monotonic()
        self.seconds: dict[str, float] = {}

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self.seconds[name] = self.seconds.get(name, 0.0) + now - self._last
        self._last = now

    def to_json(self) -> dict[str, float]:
        return {name: round(seconds, 2) for name, seconds in self.seconds.items()}


# How old a cell's list of its own sessions may be before the host is asked again (see ``RuntimeApi.session_ids``).
LISTING_MAX_AGE_S = 0.5


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


# Where each provider writes its transcript, below HOME. The watcher reads only there: a HOME also fills with what a
# provider downloads for itself (Codex clones its plugin catalogue into ``.codex/.tmp``, 8000 files and 91 MB, on some
# starts and not others; reading it was 5 to 7 of a four-cell shard's 10 to 12 CPU seconds, bench 2026-11-13), and
# Codex writes streamed text to debug SQLite logs that are not its transcript. The paths are the ones every marker was
# found under in the whole-gate bench run of 2026-11-13; Cursor's were never found in a file, so its stores are named.
# A provider that is not listed is watched over its whole HOME.
TRANSCRIPT_ROOTS: dict[str, tuple[str, ...]] = {
    "claude": (".claude/projects",),
    "codex": (".codex/sessions",),
    "antigravity": (".gemini/antigravity-cli", ".gemini/antigravity"),
    "opencode": (".local/share/opencode",),
    "pi": (".pi/agent/sessions",),
    "omp": (".omp/agent/sessions", ".local/share/omp"),
    "cursor": (".cursor/projects", ".config/cursor/chats"),
}


class FileWatcher(threading.Thread):
    """When a marker first appears in a file under the provider's HOME (its transcript, session file or store).

    Walks the tree every ``interval`` seconds, reads files whose size or mtime changed, and searches them for the markers.
    Resolution is the worst gap between completed scans (reported). The Machine Agent's own state (``.longhouse``) is
    excluded: this watches what the provider wrote, not what Longhouse did with it. No verdict reads these times: they
    say how much of a durable delay was the provider writing late. Until 2026-11-13 the walk ran 33 times a second over
    the whole HOME and was 34 of the 54 CPU seconds of a four-cell shard, more than the providers and the engine together.
    """

    # Never a transcript, and most of the files in a HOME (3700 of 4812 were under node_modules, bench 2026-11-13).
    SKIP_DIRS = {".longhouse", "node_modules", ".git", "bin"}

    def __init__(
        self, root: Path, markers: Sequence[str], stop: threading.Event, interval: float = 0.2, only_under: Sequence[str] = ()
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

    def _files(self) -> Iterator[tuple[str, os.stat_result]]:
        """Every file below the root (below ``only_under`` when set), with its stat; directories in ``SKIP_DIRS`` are not entered."""

        root = str(self.root)
        tops = [os.path.join(root, prefix) for prefix in self.only_under] if self.only_under else [root]
        while tops:
            try:
                entries = os.scandir(tops.pop())
            except OSError:
                continue
            with entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in self.SKIP_DIRS:
                                tops.append(entry.path)
                        elif entry.is_file():
                            yield entry.path, entry.stat()
                    except OSError:
                        continue

    def scan_once(self) -> None:
        pending = [m for m in self.markers if m not in self.first_seen]
        cut = len(str(self.root)) + 1
        for path, info in self._files():
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
                self.first_path[marker] = path[cut:]
            pending = [m for m in pending if m not in self.first_seen]

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.scan_once()
            self.scan_times.append(time.monotonic())
            self._stop_event.wait(self.interval)


def _frame_stage_times(data_line: str, client_wall_s: float) -> dict[str, float | None]:
    """Wall-clock seconds of each stage a stream frame passed through, from the frame's own fields.

    engine_stamp is the preview's timestamp (the Machine Agent's occurred_at), host_publish is the host's pubsub fan-out
    time, sse_yield is when the stream handler built the frame. All four are on this machine's clock in the harness.
    """

    stages: dict[str, float | None] = {"engine_stamp": None, "host_publish": None, "sse_yield": None, "client_receive": client_wall_s}
    if not data_line.startswith("data:"):
        return stages
    try:
        frame = json.loads(data_line[5:])
    except ValueError:
        return stages
    if not isinstance(frame, dict):
        return stages
    if isinstance(frame.get("server_fanout_at_ms"), int):
        stages["host_publish"] = frame["server_fanout_at_ms"] / 1000.0
    if isinstance(frame.get("server_now_ms"), int):
        stages["sse_yield"] = frame["server_now_ms"] / 1000.0
    preview = frame.get("transcript_preview")
    stamp = preview.get("timestamp") if isinstance(preview, dict) else None
    if isinstance(stamp, str):
        with contextlib.suppress(ValueError):
            stages["engine_stamp"] = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    return stages


class TimedLine(str):
    """A stream line with the monotonic and wall-clock time its reader took it off the socket."""

    received_at: float
    received_wall: float

    def __new__(cls, text: str) -> TimedLine:
        line = super().__new__(cls, text)
        line.received_at = time.monotonic()
        line.received_wall = time.time()
        return line


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
        self.marker_stages: dict[str, dict[str, float | None]] = {}  # marker -> wall-clock seconds of each stage of its first frame
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
        if isinstance(preview, dict) and preview.get("kind") not in (None, "reply", "tool"):
            return  # a thinking (or other non-reply) preview carries its own kind and is not reply text
        if isinstance(preview, dict) and not preview.get("tool_name") and isinstance(preview.get("text"), str):
            self.previews.setdefault(str(frame.get("session_id")), []).append(preview["text"])
        elif isinstance(preview, dict) and preview.get("tool_name"):
            self.tool_steps.append((str(frame.get("session_id")), str(preview["tool_name"]), str(preview.get("text") or "")))

    def run(self) -> None:
        pending = set(self.markers)
        try:
            for line in self.open_stream():
                now = getattr(line, "received_at", None) or time.monotonic()
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
                    self.marker_stages[marker] = _frame_stage_times(text, getattr(line, "received_wall", None) or time.time())
                    pending.discard(marker)
                if text.startswith("data:") and '"transcript_preview": {' in text:
                    with contextlib.suppress(ValueError):
                        frame = json.loads(text[5:])
                        preview = frame.get("transcript_preview")
                        if (
                            isinstance(preview, dict)
                            and preview.get("role") != "user"
                            and preview.get("kind") in (None, "reply")
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


class RuntimeApi:
    """The slice of the Runtime Host API the lanes read, scoped to ONE cell's sessions.

    A host shared by several cells holds every cell's sessions, and a lane that read "every session on the host" (the
    first design, with ``limit=20``) could score one cell's markers from another cell's text. A cell now reads only
    the sessions it created (``pinned_sessions``: a Console session by id) or that were launched in its own unique
    workspace directory (a Helm launch creates a managed session beside the file-discovered one, both in that cwd).
    """

    def __init__(self, api_url: str, token: str, *, workspace: Path | None = None, device_id: str | None = None) -> None:
        parsed = urlsplit(api_url)
        self.host, self.port = parsed.hostname or "127.0.0.1", parsed.port or 80
        self.token = token
        self.workspace = workspace.resolve() if workspace is not None else None
        # The list is asked for this machine's sessions only: a host shared by a whole run holds hundreds, and the list
        # returns at most 100.
        self.device_id = device_id
        self.pinned_sessions: list[str] = []
        self.known_sessions: set[str] = set()  # every id this cell's lanes ever read
        self.launched_sessions: set[str] = set()  # listed in the workspace WITH launch information: the session a launch made
        self.launched_listed_s: float | None = None  # how long wait_for_launched_session waited for one
        self.subscribed_streams: set[str] = set()  # sessions whose workspace stream the host has subscribed (first workspace_changed seen)
        self.on_wake: Callable[[str], None] | None = None  # called with the session id for every workspace_changed frame
        self._listing: list[str] = []
        self._listed_at: float | None = None
        self._listing_lock = threading.Lock()
        self.legacy_stream = True  # True: connect as a client that predates preview kinds (no `preview_kinds` parameter); the live-kinds scenarios set False so thinking frames arrive

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

    def post_raw(self, path: str, payload: dict[str, Any], *, browser: bool = False) -> tuple[int, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            headers = {**self._headers(browser), "Content-Type": "application/json"}
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

    def session_ids(self, *, fresh: bool = False) -> list[str]:
        """This cell's sessions: the pinned ones plus every session whose cwd is this cell's workspace.

        The host is asked for the list at most every ``LISTING_MAX_AGE_S`` unless ``fresh``. Every lane asks before each
        read, and until 2026-11-13 each ask was a request: a cell sent the host about 40 list requests a second on top of
        its reads, and one host could carry two cells. A wait that needs the moment a session appears passes ``fresh``.
        """

        with self._listing_lock:
            now = time.monotonic()
            if fresh or self._listed_at is None or now - self._listed_at >= LISTING_MAX_AGE_S:
                self._listing = self._list_sessions()
                self._listed_at = time.monotonic()
            ids = list(self.pinned_sessions)
            ids += [sid for sid in self._listing if sid not in ids]
        self.known_sessions.update(ids)
        return ids

    def _list_sessions(self) -> list[str]:
        ids: list[str] = []
        if self.workspace is not None:
            scope = f"&device_id={quote(self.device_id)}" if self.device_id else ""
            status, body = self.get(f"/api/agents/sessions?limit=100&include_test=true{scope}")
            if status == 200:
                for item in json.loads(body).get("sessions") or []:
                    cwd = item.get("cwd")
                    if cwd and Path(str(cwd)).resolve() == self.workspace:
                        if item.get("launch_surface"):
                            self.launched_sessions.add(str(item["id"]))
                        if str(item["id"]) not in ids:
                            ids.append(str(item["id"]))
            else:
                return list(self._listing)  # a refused list is not "no sessions": keep the last answer
        return ids

    def wait_for_launched_session(self, timeout: float = 15.0) -> bool:
        """For a cell found by its workspace, with a session already listed: wait until the LAUNCHED session is listed.

        A Helm launch makes its session on the host at once, and the provider then writes a transcript file. Until the Machine
        Agent's next periodic scan ties the two together, the list shows only a session discovered from that file, under the
        provider's own id and with no launch information; the launched session then appears and the other goes. Live frames are
        published on the launched session all along. Measured 2026-11-13 on a Linux bench, Codex Helm alone on a new host: the
        file's session listed at 4.0 s, the launched one at 10.4 s, the file's gone at 10.6 s. A cell that subscribed to the
        first and sent its prompt at 10.0 s lost a subagent start published 0.4 s later, and read it as a host drop.
        Returns False when a session is listed and the launched one did not join it in time; True otherwise (also for a cell
        with nothing listed yet, or found by id). The wait is kept in ``launched_listed_s``.
        """

        started = time.monotonic()
        if self.workspace is None or self.pinned_sessions:
            return True
        while time.monotonic() - started < timeout:
            with contextlib.suppress(Exception):
                sessions = set(self.session_ids(fresh=True))
                if not sessions:
                    return True
                if sessions & self.launched_sessions:
                    self.launched_listed_s = time.monotonic() - started
                    return True
            time.sleep(0.1)
        return False

    def wait_for_session_stream(self, timeout: float = 15.0) -> tuple[float | None, float | None]:
        """Wait for this cell's session to be listed and its workspace stream subscribed: (seconds to listed, seconds to subscribed).

        A Helm launch creates its session on the host at once, but the list this harness (and a client) finds it in did not show
        it under its working directory until the Machine Agent's next periodic scan: 9.9 s after the launch on a Machine Agent that
        had just started (runner, 2026-11-13, omp/helm alone on its own host), under 5 s on one that had been up a while. A prompt
        sent before then streams to a host with no subscriber, the host does not replay previews to a first connection, and the cell
        read "0 of 6 markers on the live stream" or a 2 s median while the host log showed every preview published within 0.2 s.
        The wait is recorded, not judged here: how long a new session takes to be listed is its own number. ``None`` means it did
        not happen within ``timeout``; the caller sends the prompt anyway and the cell fails on what the stream then misses.
        """

        started = time.monotonic()
        listed: float | None = None
        while time.monotonic() - started < timeout:
            with contextlib.suppress(Exception):
                sessions = set(self.session_ids(fresh=True))
                if sessions and listed is None:
                    listed = time.monotonic() - started
                # Found by its workspace, the cell waits for the session the launch made, not only one found from a file
                # (see wait_for_launched_session).
                launched = self.workspace is None or bool(self.pinned_sessions) or bool(sessions & self.launched_sessions)
                if launched and sessions and self.launched_listed_s is None:
                    self.launched_listed_s = time.monotonic() - started
                if sessions and launched and sessions <= self.subscribed_streams:
                    return listed, time.monotonic() - started
            time.sleep(0.1)
        return listed, None

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
            # Neither lane's time is judged: the events lane says when a turn is over and where a marker ended up, the
            # workspace lane where the live path ends. The archive read returns up to 1000 events, so it is the slower.
            self._lane("events (durable archive)", "/api/agents/sessions/{id}/events?limit=1000", browser=False, interval=0.5),
            self._lane("workspace (served to web)", "/api/timeline/sessions/{id}/workspace", browser=True, interval=0.5),
        ]

    def _stream_one(self, session: str, out: queue.Queue[TimedLine | str], stop: threading.Event) -> None:
        while not stop.is_set():
            connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
            try:
                headers = self._headers(True)
                headers["Accept"] = "text/event-stream"
                # The new web client asks for thinking previews; a connection that does not is never sent them (`legacy_stream`).
                query = "" if self.legacy_stream else "?preview_kinds=thinking"
                connection.request("GET", f"/api/timeline/sessions/{session}/workspace/stream{query}", headers=headers)
                response = connection.getresponse()
                if response.status != 200:
                    out.put(TimedLine(f"harness: workspace stream HTTP {response.status} for {session}\n"))
                    return
                while not stop.is_set():
                    raw = response.readline()
                    if not raw:
                        break
                    if raw.startswith(b"event: workspace_changed"):
                        # The host sends this first one from inside its subscription, so the stream is attached from here on.
                        self.subscribed_streams.add(session)
                        if self.on_wake is not None:
                            self.on_wake(session)
                    out.put(TimedLine(raw.decode("utf-8", errors="replace")))
            except OSError:
                pass
            finally:
                connection.close()
            stop.wait(0.2)

    def stream_lines(self, stop: threading.Event) -> Iterator[str]:
        """The workspace SSE (what the web client subscribes to) of every session, as lines, as each session appears.

        Each line is a ``TimedLine`` stamped when the reader thread took it off the socket. Finding new sessions is a
        blocking HTTP GET against the same host being measured; it runs on its own thread so that a slow host answering
        it can never hold back lines that already arrived (it did: the median read 5 s on a loaded runner while the
        frames were on the wire).
        """

        out: queue.Queue[TimedLine | str] = queue.Queue()
        started: set[str] = set()

        def scan() -> None:
            while not stop.is_set():
                try:
                    for session in self.session_ids():
                        if session not in started:
                            started.add(session)
                            threading.Thread(target=self._stream_one, args=(session, out, stop), daemon=True).start()
                except Exception:  # noqa: BLE001 - a failed scan is retried; a stream error is reported by its own reader
                    pass
                stop.wait(0.1)

        threading.Thread(target=scan, daemon=True, name="lane-stream-scan").start()
        while not stop.is_set():
            try:
                yield out.get(timeout=0.05)
            except queue.Empty:
                continue


def state_snapshot(workspace_body: str, session_id: str) -> dict[str, Any] | None:
    """The state facts a web client reads from one workspace response, reduced to what the runtime strip draws.

    ``None`` when the body has no ``session.session_state`` (an observer that cannot see the facts must say so, never report "absent").
    ``headline`` is ``presentation.primary.label`` ("Thinking", "Using Bash"); ``detail`` repeats ``delegatedWorkDetail`` in
    ``web/src/features/session/SessionRuntimeStrip.tsx`` ("<subagent name> . Using <tool>"), the string the strip and the Mac menu bar show for a
    delegated subagent.
    """

    try:
        body = json.loads(workspace_body)
    except ValueError:
        return None
    session = body.get("session") if isinstance(body, dict) else None
    facts = session.get("session_state") if isinstance(session, dict) else None
    if not isinstance(facts, dict) or not isinstance(facts.get("activity"), dict):
        return None
    activity = facts["activity"]
    primary = (facts.get("presentation") or {}).get("primary") or {}
    delegation = facts.get("delegation") or {}
    tasks: list[dict[str, Any]] = []
    for item in delegation.get("items") or []:
        progress = item.get("native_progress") or {}
        tasks.append(
            {
                "name": (item.get("description") or "").strip() or item.get("id"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "current_tool": (progress.get("current_tool") or "").strip() or None,
                "progress_status": progress.get("status"),
            }
        )
    lead = next((t for t in tasks if t["kind"] == "subagent"), tasks[0] if tasks else None)
    detail = None
    if lead:
        step = (
            f"Using {lead['current_tool']}"
            if lead["current_tool"]
            else "Thinking"
            if str(lead["progress_status"] or "").lower() == "thinking"
            else None
        )
        detail = " . ".join(x for x in (lead["name"], step) if x) or None
    return {
        "session_id": session_id,
        "headline_key": primary.get("key"),
        "headline": primary.get("label"),
        "activity_state": activity.get("state"),
        "activity_tool": activity.get("tool"),
        "activity_source": activity.get("source"),
        "delegation_state": delegation.get("state"),
        "delegation_count": delegation.get("count") or 0,
        "tasks": tasks,
        "detail": detail,
        "commit_seq": facts.get("commit_seq"),
    }


STATE_KEY = ("headline", "activity_state", "activity_tool", "delegation_count", "detail")


class StateObserver(threading.Thread):
    """The session state facts as the web client reads them: ``GET /api/timeline/sessions/{id}/workspace`` (browser auth).

    This is the lane behind "Thinking", "Using Bash" and "<subagent> . Using Read" in the runtime strip and the menu bar. The client
    refetches that response on every stream wake, so polling it at ``interval`` sees what a client sees, up to the poll gap (reported).
    Every change of what the strip would draw is kept with its time; ``reads`` and ``shaped`` count responses and how many carried the
    facts, so an observer that read nothing is distinguishable from a session in which nothing happened.

    Five reads a second. A workspace read costs the host about 50 ms of CPU, so ten a second (the rate until 2026-11-13) was
    half a core of host for every cell, and the hosts were then the largest user of CPU in a run. The budgets these times are
    judged against are 1.0 s (``WOKEN_STATE_BUDGET_S``) and half a state's window.
    """

    def __init__(self, api: RuntimeApi, stop: threading.Event, interval: float = 0.2) -> None:
        super().__init__(daemon=True, name="lane-state-facts")
        self.api, self.interval, self._stop_event = api, interval, stop
        self.timeline: list[tuple[float, dict[str, Any]]] = []
        self.reads = 0
        self.shaped = 0
        self.errors: list[str] = []
        self.read_times: list[float] = []
        self.gone: dict[str, float] = {}  # session id -> when the host stopped serving it (unlisted, or a 404), if it has not come back
        self._last: dict[str, tuple] = {}

    @property
    def resolution_s(self) -> float | None:
        gaps = [b - a for a, b in zip(self.read_times, self.read_times[1:], strict=False)]
        return max(gaps) if gaps else None

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                listed = self.api.session_ids()
                now = time.monotonic()
                for sid in self._last:
                    if sid not in listed:
                        self.gone.setdefault(sid, now)
                for sid in listed:
                    status, body = self.api.get(f"/api/timeline/sessions/{sid}/workspace", browser=True)
                    now = time.monotonic()
                    self.reads += 1
                    self.read_times.append(now)
                    if status == 404:
                        # Listed a moment ago and gone now: a Helm session found from the provider's file goes when the launched
                        # one is tied to it. Its last state stopped being served here; the other sessions are still read.
                        self.gone.setdefault(sid, now)
                        if len(self.errors) < 10:
                            self.errors.append("RuntimeError: HTTP 404")
                        continue
                    if status != 200:
                        raise RuntimeError(f"HTTP {status}")
                    self.gone.pop(sid, None)
                    snap = state_snapshot(body, sid)
                    if snap is None:
                        continue
                    self.shaped += 1
                    key = tuple(json.dumps(snap.get(k), sort_keys=True) for k in STATE_KEY)
                    if self._last.get(sid) != key:
                        self._last[sid] = key
                        self.timeline.append((now, snap))
            except Exception as exc:  # noqa: BLE001 - an observer failure is data
                if len(self.errors) < 10:
                    self.errors.append(f"{type(exc).__name__}: {exc}")
            self._stop_event.wait(self.interval)


class WokenStateReader(threading.Thread):
    """The session state facts as a client that reads only when the stream wakes it is left showing them.

    The web and iOS clients refetch the workspace when the stream sends a frame and at no other time while the stream is healthy.
    ``StateObserver`` polls, so it sees a state the host serves and never announces; this reader does not. One read in flight, and a
    wake that lands during it runs one more: the web client's own rule, which is what makes a frame sent before the state is readable
    cost that client the state. 2026-11-12: the host served "Using run_command" 0.9 s after the frame that should have carried it
    and sent nothing when it did, so the page said "Thinking" for the whole tool run while the poller reported the lane live.
    """

    def __init__(self, api: RuntimeApi, stop: threading.Event) -> None:
        super().__init__(daemon=True, name="lane-state-woken")
        self.api, self._stop_event = api, stop
        self.timeline: list[tuple[float, dict[str, Any]]] = []  # every shaped read, in order
        self.wakes = 0
        self.reads = 0
        self.errors: list[str] = []
        self._dirty: list[str] = []
        self._cond = threading.Condition()

    def wake(self, session: str) -> None:
        with self._cond:
            self.wakes += 1
            if session not in self._dirty:
                self._dirty.append(session)
            self._cond.notify()

    def run(self) -> None:
        while not self._stop_event.is_set():
            with self._cond:
                if not self._dirty:
                    self._cond.wait(0.1)
                    continue
                sid = self._dirty.pop(0)
            try:
                status, body = self.api.get(f"/api/timeline/sessions/{sid}/workspace", browser=True)
                now = time.monotonic()
                self.reads += 1
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
                snap = state_snapshot(body, sid)
                if snap is not None:
                    self.timeline.append((now, snap))
            except Exception as exc:  # noqa: BLE001 - an observer failure is data
                if len(self.errors) < 10:
                    self.errors.append(f"{type(exc).__name__}: {exc}")


def _strip_text(snap: dict[str, Any]) -> tuple[Any, Any]:
    return snap.get("headline"), snap.get("detail")


def woken_view_lags(
    polled: Sequence[tuple[float, dict[str, Any]]],
    woken: Sequence[tuple[float, dict[str, Any]]],
    *,
    since: float,
    end_t: float,
    gone: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """For each state the host served from ``since`` on: how long a woken-only client took to show it, or how long it went unshown.

    ``polled`` is ``StateObserver.timeline`` (one entry per change), ``woken`` is ``WokenStateReader.timeline`` (one per read). A state
    is what the runtime strip draws: the headline and the delegated-work detail. ``lag_s`` is from the poller first seeing the state to
    the woken reader holding it; a state the reader never held before the host moved on (or the run ended) has ``shown`` false and
    ``lag_s`` equal to the time it was served. A state served for less than the lag budget cannot fail it, so a blip is not a finding.
    ``gone`` is ``StateObserver.gone``: a session the host stopped serving had its last state served until then, not until the run
    ended. 2026-11-13: a Codex Helm session found from the provider's file was read once as "Idle" 0.25 s after the prompt and
    removed 0.6 s later, and its "Idle" was counted as served and unshown for the remaining 24.4 s of the run.
    """

    out: list[dict[str, Any]] = []
    for sid in sorted({snap.get("session_id") for _, snap in polled if snap.get("session_id")}):
        changes: list[tuple[float, tuple[Any, Any]]] = []
        for t, snap in polled:
            if snap.get("session_id") == sid and (not changes or changes[-1][1] != _strip_text(snap)):
                changes.append((t, _strip_text(snap)))
        reads = [(t, _strip_text(snap)) for t, snap in woken if snap.get("session_id") == sid]
        for index, (t, text) in enumerate(changes):
            if t < since:
                continue
            until = changes[index + 1][0] if index + 1 < len(changes) else max(t, min(end_t, (gone or {}).get(sid, end_t)))
            # The reader may have read the state a moment before the poller's next tick did.
            window_from = changes[index - 1][0] if index else float("-inf")
            held_before = [text_r for t_r, text_r in reads if window_from <= t_r <= t]
            if held_before and held_before[-1] == text:
                shown_at: float | None = t
            else:
                shown_at = next((t_r for t_r, text_r in reads if t < t_r < until and text_r == text), None)
            out.append(
                {
                    "session_id": sid,
                    "headline": text[0],
                    "detail": text[1],
                    "served_at": t,
                    "served_for_s": round(max(0.0, until - t), 3),
                    "shown": shown_at is not None,
                    "lag_s": round(max(0.0, (shown_at if shown_at is not None else until) - t), 3),
                }
            )
    return out


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
        # What each marker-bearing runtime event looked like on the wire (source, progress_kind, raw event type): the
        # evidence for "engine posted it" and for which host path has to read it. First 600 events only.
        self.runtime_samples: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._closing = threading.Event()
        # Seconds each chunk is held, each way, between the engine and the host (``--engine-delay-ms``). A loopback host
        # answers a post in milliseconds, so the engine never has two things queued behind one post in flight, and a tool
        # call never lands behind reply text stamped after it; over a real network to a hosted tenant both happen.
        self.delay_s = 0.0

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
        delayed: queue.Queue[tuple[float, bytes] | None] | None = None
        sender: threading.Thread | None = None
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                if scanner is not None:
                    scanner.feed(data, time.monotonic())  # the engine's own send time, before any added delay
                if self.delay_s <= 0 and delayed is None:
                    sink.sendall(data)
                    continue
                if delayed is None:
                    delayed = queue.Queue()
                    sender = threading.Thread(target=self._send_delayed, args=(delayed, sink), daemon=True)
                    sender.start()
                delayed.put((time.monotonic() + self.delay_s, data))
        except OSError:
            pass
        finally:
            if delayed is not None and sender is not None:
                delayed.put(None)  # the bytes already taken leave before the sockets close
                sender.join(timeout=self.delay_s + 5)
            for sock in (sink, source):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _send_delayed(pending: queue.Queue[tuple[float, bytes] | None], sink: socket.socket) -> None:
        """Send each chunk at its due time, in order: added latency, not a throughput limit."""

        while (item := pending.get()) is not None:
            due, chunk = item
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                sink.sendall(chunk)
            except OSError:
                return

    def _sample(self, path: bytes, body: bytes, now: float) -> None:
        if not path.startswith(b"/api/agents/runtime/") or not self.MARKER.search(body) or len(self.runtime_samples) >= 600:
            return
        try:
            batch = json.loads(body)
        except ValueError:
            return
        events = batch.get("events") if isinstance(batch, dict) else None
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, dict) or not self.MARKER.search(json.dumps(event).encode()):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            raw = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            inner = raw.get("event") if isinstance(raw.get("event"), dict) else {}
            self.runtime_samples.append(
                {
                    "t": now,
                    "source": event.get("source"),
                    "kind": event.get("kind"),
                    "progress_kind": payload.get("progress_kind"),
                    "raw_type": raw.get("type") or raw.get("method"),
                    "inner_type": inner.get("type") or (inner.get("delta") or {}).get("type") if inner else None,
                    "payload_keys": sorted(payload)[:16],
                    "markers": sorted({m.decode() for m in self.MARKER.findall(json.dumps(event).encode())})[:4],
                }
            )

    def _note(self, path: bytes, body: bytes, now: float) -> None:
        self._sample(path, body, now)
        for found in self.MARKER.finditer(body):
            marker = found.group().decode()
            book = self.runtime_posts if path.startswith(b"/api/agents/runtime/") else self.other_posts
            with self._lock:
                book.setdefault(marker, now)


_PUBLISH_LINE = re.compile(r"live_transcript publish session=(\S+) seq=\S+ age_ms=\S+ text_len=(\d+)")


def host_publishes(log: Path, sessions: Iterable[str]) -> tuple[int, int, int]:
    """(publish lines, longest text_len, runtime-events requests) the host logged for this cell's sessions.

    The requests are every machine's on a host shared by a run: a count of the host's load, not of this cell.
    """

    wanted = set(sessions)
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


def pacing_error(schedule: Sequence[tuple[int, str, float]], terminal: dict[str, float | None]) -> float | None:
    """Median |terminal spacing - scheduled spacing| from the first marker: did the terminal stream live?"""

    seen = [(due, terminal[word]) for _, word, due in schedule if terminal.get(word) is not None]
    if len(seen) < 2:
        return None
    base_due, base_t = seen[0]
    return statistics.median(abs((t - base_t) - (due - base_due)) for due, t in seen[1:])  # type: ignore[operator]
