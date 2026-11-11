#!/usr/bin/env python3
"""Stand-in provider TUI for the terminal mirror spike (no model, no network).

Draws what a coding-agent TUI draws: a title row, a reply that streams in word
by word inside a scroll region, a spinner/status row, and a bordered input box
at the bottom that redraws. It can run a full-screen repaint and switch to the
alternate screen, and it redraws itself on SIGWINCH.

Every output chunk carries a latency marker `[M<seq:06d> <epoch us:016d>]`
(unless --no-markers) stamped just before the write to the PTY. Phase changes
and checkpoints are appended to the file named by --events (JSON lines), and
each marker's phase to --markers, so a harness can line the run up with the
bytes it recorded. A checkpoint pauses output for --pause seconds so a harness
can compare screens while nothing moves.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import termios
import time
import tty

parser = argparse.ArgumentParser()
parser.add_argument("--events", required=True)
parser.add_argument("--markers", required=True)
parser.add_argument("--no-markers", action="store_true")
parser.add_argument("--pause", type=float, default=5.0)
parser.add_argument("--stream-seconds", type=float, default=10.0)
parser.add_argument("--idle-seconds", type=float, default=6.0)
parser.add_argument("--redraw-seconds", type=float, default=3.0)
parser.add_argument("--alt-seconds", type=float, default=4.0)
parser.add_argument("--end-after-checkpoint", default="after-input")
args = parser.parse_args()

OUT = 1
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WORDS = (
    "the relay copies every byte the provider draws into a bounded queue and a "
    "sender thread forwards them so the browser can paint the same screen while "
    "the owner keeps typing in their own terminal and nothing waits for anyone"
).split()

events = open(args.events, "a", buffering=1)
marker_log: list[str] = []
seq = 0
phase = "boot"
resized = False
size = os.get_terminal_size(OUT)
typed = ""
text_col = 0


def now_us() -> int:
    return time.time_ns() // 1000


def event(kind: str, name: str) -> None:
    events.write(json.dumps({"t": now_us(), "kind": kind, "name": name}) + "\n")


def on_winch(*_):
    global resized
    resized = True


signal.signal(signal.SIGWINCH, on_winch)


def marker() -> str:
    """Next marker text; the stamp is taken here, just before the write."""
    global seq
    if args.no_markers:
        return ""
    seq += 1
    t = now_us()
    marker_log.append(json.dumps({"seq": seq, "t": t, "phase": phase}))
    return f" [M{seq:06d} {t:016d}]"


def write(data: str) -> None:
    raw = data.encode()
    while raw:
        raw = raw[os.write(OUT, raw):]


def goto(row: int, col: int = 1) -> str:
    return f"\x1b[{row};{col}H"


def title(cols: int, rows: int) -> str:
    text = f" standin-tui  model=fake  {cols}x{rows}  phase={phase} "
    return goto(1) + "\x1b[7m" + text.ljust(cols)[:cols] + "\x1b[0m"


def box(cols: int, rows: int) -> str:
    inner = max(cols - 2, 1)
    line = (typed[-(inner - 4):] if typed else "") + "_"
    out = goto(rows - 3) + "\x1b[36m┌" + "─" * inner + "┐\x1b[0m"
    out += goto(rows - 2) + "\x1b[36m│\x1b[0m " + ("> " + line).ljust(inner - 1)[: inner - 1] + "\x1b[36m│\x1b[0m"
    out += goto(rows - 1) + "\x1b[36m│\x1b[0m\x1b[2m " + "enter to send  ctrl-c to quit".ljust(inner - 1)[: inner - 1] + "\x1b[0m\x1b[36m│\x1b[0m"
    out += goto(rows) + "\x1b[36m└" + "─" * inner + "┘\x1b[0m"
    return out


def status(cols: int, rows: int, started: float, label: str) -> str:
    frame = SPIN[int((time.time() - started) * 12) % len(SPIN)]
    text = f"\x1b[33m{frame}\x1b[0m {label} {time.time() - started:5.1f}s{marker()}"
    return goto(rows - 4) + text + "\x1b[K"


def draw_all(streamed_lines: list[str]) -> None:
    """Repaint the whole screen at the current size (boot, resize)."""
    global size, text_col
    size = os.get_terminal_size(OUT)
    cols, rows = size.columns, size.lines
    out = "\x1b[?25l\x1b[0m\x1b[2J" + f"\x1b[2;{rows - 5}r" + title(cols, rows)
    region = rows - 6
    shown = streamed_lines[-region:]
    for index, line in enumerate(shown):
        out += goto(2 + index) + line[: cols - 1]
    out += status(cols, rows, time.time(), "ready") + box(cols, rows)
    # Put the stream cursor at the end of the last streamed line, or the
    # bottom of the region when it is empty.
    last = shown[-1] if shown else ""
    out += goto(2 + max(len(shown) - 1, 0), min(len(last), cols - 1) + 1)
    text_col = len(last)
    write(out + marker_line())


def marker_line() -> str:
    return ""


class Stream:
    def __init__(self):
        self.lines: list[str] = [""]
        self.index = 0

    def next_word(self, cols: int) -> str:
        word = WORDS[self.index % len(WORDS)] + " "
        self.index += 1
        return word


stream = Stream()


def feed_word(cols: int, rows: int) -> str:
    """Append one word at the cursor, wrapping and scrolling inside the region."""
    global text_col
    word = stream.next_word(cols)
    out = ""
    if text_col + len(word) > cols - 1:
        out += "\r\n"
        stream.lines.append("")
        text_col = 0
    stream.lines[-1] += word
    text_col += len(word)
    return out + word


input_changed = False
counts = {"n": 0}


def pump_input() -> None:
    global typed, input_changed
    while select.select([0], [], [], 0)[0]:
        data = os.read(0, 4096)
        if not data:
            return
        input_changed = True
        for ch in data.decode(errors="ignore"):
            if ch in "\r\n":
                if typed:
                    event("input", typed)
                    stream.lines.append("you: " + typed)
                typed = ""
            elif ch in "\x7f\b":
                typed = typed[:-1]
            elif ch == "\x03":
                raise SystemExit(0)
            elif ch.isprintable():
                typed += ch


def tick_sleep(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(0.01, max(0.0, end - time.time())))
        pump_input()


def checkpoint(name: str) -> None:
    event("checkpoint", name)
    end = time.time() + args.pause
    while time.time() < end:
        time.sleep(0.02)
        pump_input()
        global input_changed
        if resized:
            handle_resize()
        elif input_changed:
            input_changed = False
            draw_all(stream.lines)


def handle_resize() -> None:
    global resized
    resized = False
    draw_all(stream.lines)
    event("redrawn", f"{size.columns}x{size.lines}")


def set_phase(name: str) -> None:
    global phase
    phase = name
    counts["n"] = 0
    event("phase", name)


def run_phase_stream(seconds: float) -> None:
    set_phase("stream")
    started = time.time()
    end = started + seconds
    n = 0
    while time.time() < end:
        if resized:
            handle_resize()
        pump_input()
        cols, rows = size.columns, size.lines
        out = feed_word(cols, rows) + "\x1b7" + status(cols, rows, started, "Generating")
        if n % 5 == 0:
            out += box(cols, rows)
        write(out + "\x1b8")
        n += 1
        tick_sleep(0.05)
    event("count", f"stream:{n}")


def run_phase_idle(seconds: float) -> None:
    set_phase("idle-spinner")
    started = time.time()
    end = started + seconds
    while time.time() < end:
        if resized:
            handle_resize()
        pump_input()
        cols, rows = size.columns, size.lines
        write("\x1b7" + status(cols, rows, started, "Waiting for tool") + "\x1b8")
        counts["n"] += 1
        tick_sleep(0.08)
    event("count", f"idle-spinner:{counts['n']}")


def full_screen(cols: int, rows: int, n: int) -> str:
    out = "\x1b[?25l\x1b[0m\x1b[H"
    for row in range(1, rows + 1):
        shade = 31 + (row + n) % 7
        text = f" row {row:02d} frame {n:04d}{marker() if row == 1 else ''} " + ("0123456789abcdef" * 12)
        out += goto(row) + f"\x1b[{shade}m" + text.ljust(cols)[:cols] + "\x1b[0m"
    return out


def run_phase_redraw(seconds: float) -> None:
    set_phase("full-redraw")
    end = time.time() + seconds
    n = 0
    while time.time() < end:
        if resized:
            handle_resize()
        pump_input()
        write(full_screen(size.columns, size.lines, n))
        n += 1
        tick_sleep(0.2)
    event("count", f"full-redraw:{n}")
    draw_all(stream.lines)


def run_phase_alt(seconds: float) -> None:
    set_phase("alt-screen")
    write("\x1b[?1049h\x1b[2J")
    started = time.time()
    end = started + seconds
    n = 0
    while time.time() < end:
        if resized:
            size_now = os.get_terminal_size(OUT)
            globals()["size"] = size_now
            globals()["resized"] = False
        cols, rows = size.columns, size.lines
        out = "\x1b[H\x1b[0m\x1b[7m" + " ALT SCREEN MENU ".center(cols)[:cols] + "\x1b[0m"
        for row in range(3, rows - 1):
            mark = ">" if (row + n // 5) % 9 == 0 else " "
            out += goto(row) + f" {mark} option {row:02d}: alternate screen entry".ljust(cols)[:cols]
        out += goto(rows) + f" menu tick {n:04d}{marker()}\x1b[K"
        write(out)
        n += 1
        tick_sleep(0.1)
    event("count", f"alt-screen:{n}")


def main() -> None:
    saved = termios.tcgetattr(0)
    tty.setraw(0)
    try:
        set_phase("boot")
        draw_all(stream.lines)
        tick_sleep(0.8)
        run_phase_stream(args.stream_seconds)
        checkpoint("after-stream")
        run_phase_idle(args.idle_seconds)
        run_phase_redraw(args.redraw_seconds)
        run_phase_alt(args.alt_seconds)
        checkpoint("alt-screen")
        write("\x1b[?1049l")
        set_phase("after-alt")
        # No repaint: leaving the alternate screen restores the main screen,
        # as less or vim rely on.
        checkpoint("after-alt")
        # The harness resizes the owner's terminal during this pause.
        checkpoint("after-resize")
        checkpoint("after-input")
        set_phase("phone-resize")
        checkpoint("phone-resize")
        write("\x1b[?25h\r\n" + "bye\r\n")
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        with open(args.markers, "a") as handle:
            handle.write("\n".join(marker_log) + "\n")
        event("exit", "done")


if __name__ == "__main__":
    main()
