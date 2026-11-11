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

from zerg.qa.pty_reader import TimedChunk

_HISTORY_LINES = 20_000


@dataclass(frozen=True)
class FirstSeen:
    needle: str
    t: float | None  # time.monotonic() of the chunk that first showed it; None: never on screen
    chunk_index: int | None


def _row_text(row: dict[int, pyte.screens.Char], columns: int) -> str:
    return "".join(row[column].data if column in row else " " for column in range(columns))


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

    def _haystacks(self) -> list[str]:
        display = [line.rstrip() for line in self._screen.display]
        history = self._screen.history.top
        scrolled = [_row_text(row, self.columns).rstrip() for row in list(history)[self._history_seen :]]
        self._history_seen = len(history)
        rows = scrolled + display
        # Unwrapped too: a long word that wrapped at the right edge is one word to the reader.
        return ["\n".join(rows), "".join(rows)]

    def feed(self, t: float, data: bytes) -> None:
        self._stream.feed(data)
        index = self._chunks
        self._chunks += 1
        if not self._pending:
            return
        haystacks = self._haystacks()
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
