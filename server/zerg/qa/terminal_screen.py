"""Turn timestamped PTY bytes into "this text was first on screen at time T".

A terminal emulator (pyte) replays the provider's raw output chunk by chunk, so cursor
moves, in-place redraws (spinners, bottom input boxes) and line wraps are resolved by the
emulator and nothing here parses escape sequences. After each chunk the visible screen
and the lines that just scrolled off it are searched for the needles; a needle's time is
the monotonic timestamp of the first chunk after which it was present.

Resolution is the PTY chunk: one `os.read` of up to 64 KiB, so a needle is dated to the
read that delivered it, not to the byte inside it.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass

import pyte
from wcwidth import wcwidth

from zerg.qa.pty_reader import TimedChunk

_HISTORY_LINES = 20_000


@dataclass(frozen=True)
class FirstSeen:
    needle: str
    t: float | None  # time.monotonic() of the chunk that first showed it; None: never on screen
    chunk_index: int | None


def _row_text(row: dict[int, pyte.screens.Char], columns: int) -> str:
    return "".join(row[column].data if column in row else " " for column in range(columns))


def _display_line(line: dict[int, pyte.screens.Char], columns: int) -> str:
    """One screen line as ``pyte.Screen.display`` renders it (the cell after a wide character is its stub and is skipped)."""

    out: list[str] = []
    stub = False
    for column in range(columns):
        if stub:
            stub = False
            continue
        char = line[column].data
        # A stub left behind when its wide character was erased or shifted holds no text; pyte's own display raises on it.
        stub = bool(char) and wcwidth(char[0]) == 2
        out.append(char)
    return "".join(out)


class ScreenReplay:
    """Feed timestamped chunks; read back when each needle first became visible."""

    def __init__(self, needles: Sequence[str], *, rows: int = 40, columns: int = 132) -> None:
        if any(not needle for needle in needles):
            raise ValueError("needles must be non-empty")
        self.rows = rows
        self.columns = columns
        self._screen = pyte.HistoryScreen(columns, rows, history=_HISTORY_LINES, ratio=1.0)
        self._stream = pyte.ByteStream(self._screen)
        self._pending = list(dict.fromkeys(needles))
        self._found: dict[str, tuple[float, int]] = {}
        self._history_seen = 0
        self._chunks = 0
        # The visible lines as last rendered. Only the lines the emulator marks as changed are rendered again: rendering
        # all of them after every chunk was 13 of the 18 CPU seconds a four-cell shard's main thread used (2026-11-13).
        self._lines: list[str] = [""] * rows
        self._screen.dirty.update(range(rows))

    def _haystacks(self) -> list[str] | None:
        """The text to search after a chunk, or None when the chunk changed nothing a reader could see."""

        screen = self._screen
        history = screen.history.top
        changed = bool(screen.dirty) or len(history) != self._history_seen
        if not changed:
            return None
        for row in screen.dirty:
            if 0 <= row < self.rows:
                self._lines[row] = _display_line(screen.buffer[row], self.columns).rstrip()
        screen.dirty.clear()
        scrolled = [_row_text(row, self.columns).rstrip() for row in list(history)[self._history_seen :]]
        self._history_seen = len(history)
        rows = scrolled + self._lines
        # Unwrapped too: a long word that wrapped at the right edge is one word to the reader.
        return ["\n".join(rows), "".join(rows)]

    def feed(self, t: float, data: bytes) -> None:
        self._stream.feed(data)
        index = self._chunks
        self._chunks += 1
        if not self._pending:
            return
        haystacks = self._haystacks()
        if haystacks is None:  # what was searched after the last chunk is still what is there
            return
        still = []
        for needle in self._pending:
            if any(needle in haystack for haystack in haystacks):
                self._found[needle] = (t, index)
            else:
                still.append(needle)
        self._pending = still

    def result(self, needles: Iterable[str]) -> list[FirstSeen]:
        return [FirstSeen(n, *self._found[n]) if n in self._found else FirstSeen(n, None, None) for n in needles]

    @property
    def display(self) -> list[str]:
        return [line.rstrip() for line in self._screen.display]


def first_visible(
    chunks: Iterable[TimedChunk | tuple[float, bytes]],
    needles: Sequence[str],
    *,
    rows: int = 40,
    columns: int = 132,
) -> list[FirstSeen]:
    """When each needle first appeared on a `rows` x `columns` screen fed by `chunks`."""

    replay = ScreenReplay(needles, rows=rows, columns=columns)
    for chunk in chunks:
        t, data = (chunk.t, chunk.data) if isinstance(chunk, TimedChunk) else chunk
        replay.feed(t, data)
    return replay.result(needles)
