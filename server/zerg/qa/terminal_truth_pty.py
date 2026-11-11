"""PTY and screen: the owned terminal, its replay into text, readiness, and the harness's own check (fake TUI)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import statistics
import sys
import threading
import time
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.qa.pty_reader import TimedChunk
from zerg.qa.pty_reader import load_timed_chunks
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.terminal_screen import ScreenReplay
from zerg.qa.terminal_truth_lanes import Lane
from zerg.qa.terminal_truth_lanes import LaneStats
from zerg.qa.terminal_truth_lanes import LaneWatcher
from zerg.qa.terminal_truth_lanes import lane_stats

SERVER_ROOT = Path(__file__).resolve().parents[2]


REPO_ROOT = SERVER_ROOT.parent


# Short on purpose: managed Helm binds unix sockets under HOME and macOS caps a socket path at 104 bytes.
SCRATCH_ROOT = Path("/tmp/agents/tt")


ROWS, COLUMNS = 40, 132


PROMPT = "TTRUTH please stream the long reply"


def terminal_first_seen(chunks: Iterable[TimedChunk | tuple[float, bytes]], markers: Sequence[str]) -> dict[str, float | None]:
    replay = ScreenReplay(markers, rows=ROWS, columns=COLUMNS)
    for chunk in chunks:
        t, data = (chunk.t, chunk.data) if isinstance(chunk, TimedChunk) else chunk
        replay.feed(t, data)
    return {r.needle: r.t for r in replay.result(markers)}


def screen_text(chunks: Iterable[TimedChunk]) -> str:
    replay = ScreenReplay([], rows=ROWS, columns=COLUMNS)
    for chunk in chunks:
        replay.feed(chunk.t, chunk.data)
    return "\n".join(replay.display)


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


def _new_scratch() -> Path:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        path = SCRATCH_ROOT / secrets.token_hex(2)
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue


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
