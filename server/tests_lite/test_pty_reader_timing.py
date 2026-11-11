"""Per-chunk monotonic timestamps beside the raw PTY recording."""

from __future__ import annotations

import os
import pty
import sys
import time
from pathlib import Path

import pytest

from zerg.qa.pty_reader import OwnedPtyReader
from zerg.qa.pty_reader import load_timed_chunks
from zerg.qa.pty_session import ProviderPtySession


def _wait_for(path: Path, size: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while (not path.exists() or path.stat().st_size < size) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert path.exists() and path.stat().st_size >= size


def test_each_chunk_gets_a_monotonic_timestamp_and_joins_back_to_the_raw_bytes(tmp_path: Path) -> None:
    master_fd, slave_fd = pty.openpty()
    recording, timing = tmp_path / "t.raw", tmp_path / "t.timing"
    reader = OwnedPtyReader.start(master_fd, recording, mode="ab", timing_path=timing)
    pieces = [b"alpha", b"bravo", b"charlie"]
    sent_at = []
    try:
        total = 0
        for piece in pieces:
            sent_at.append(time.monotonic())
            os.write(slave_fd, piece)
            total += len(piece)
            _wait_for(recording, total)
            time.sleep(0.15)
    finally:
        os.close(slave_fd)
        reader.close(timeout=0.5)
        os.close(master_fd)
    chunks = load_timed_chunks(recording, timing)
    assert b"".join(c.data for c in chunks) == b"".join(pieces) == recording.read_bytes()
    assert [c.offset for c in chunks] == sorted(c.offset for c in chunks)
    times = [c.t for c in chunks]
    assert times == sorted(times)
    # Each piece was read within a few ms of being written, on the same monotonic clock.
    for piece, sent, chunk in zip(pieces, sent_at, chunks, strict=True):
        assert chunk.data == piece
        assert 0 <= chunk.t - sent < 0.1
    assert times[2] - times[0] >= 0.28


def test_without_a_timing_path_nothing_extra_is_written_and_the_recording_is_identical(tmp_path: Path) -> None:
    results = {}
    for name, timing in (("plain", None), ("timed", tmp_path / "timed.timing")):
        master_fd, slave_fd = pty.openpty()
        recording = tmp_path / f"{name}.raw"
        reader = OwnedPtyReader.start(master_fd, recording, mode="ab", timing_path=timing)
        try:
            os.write(slave_fd, b"same bytes\r\n")
            _wait_for(recording, 12)
        finally:
            os.close(slave_fd)
            reader.close(timeout=0.5)
            os.close(master_fd)
        results[name] = recording.read_bytes()
    assert results["plain"] == results["timed"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["plain.raw", "timed.raw", "timed.timing"]


def test_an_incomplete_recording_takes_its_timing_sidecar_with_it(tmp_path: Path) -> None:
    master_fd, slave_fd = pty.openpty()
    recording, timing = tmp_path / "t.raw", tmp_path / "t.timing"
    reader = OwnedPtyReader.start(master_fd, recording, mode="ab", timing_path=timing)
    try:
        os.write(slave_fd, b"x")
        _wait_for(recording, 1)
        reader.mark_incomplete()
        time.sleep(0.05)
        assert not recording.exists() and not timing.exists()
    finally:
        os.close(slave_fd)
        reader.close(timeout=0.0)
        os.close(master_fd)


def test_appending_to_an_existing_recording_keeps_offsets_absolute(tmp_path: Path) -> None:
    recording, timing = tmp_path / "t.raw", tmp_path / "t.timing"
    recording.write_bytes(b"EARLIER")
    master_fd, slave_fd = pty.openpty()
    reader = OwnedPtyReader.start(master_fd, recording, mode="ab", timing_path=timing)
    try:
        os.write(slave_fd, b"new")
        _wait_for(recording, 10)
    finally:
        os.close(slave_fd)
        reader.close(timeout=0.5)
        os.close(master_fd)
    chunks = load_timed_chunks(recording, timing)
    assert [(c.offset, c.data) for c in chunks] == [(7, b"new")]


def test_provider_session_records_timing_for_a_real_child(tmp_path: Path) -> None:
    script = "import sys,time\nfor w in ('one','two','three'):\n    sys.stdout.write(w+'\\n'); sys.stdout.flush(); time.sleep(0.2)\n"
    recording, timing = tmp_path / "t.raw", tmp_path / "t.timing"
    started = time.monotonic()
    session = ProviderPtySession.start(
        argv=[sys.executable, "-c", script],
        cwd=tmp_path,
        env=dict(os.environ),
        terminal_path=recording,
        timing_path=timing,
    )
    try:
        deadline = time.monotonic() + 5
        while session.alive() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        session.close()
    chunks = load_timed_chunks(recording, timing)
    text = b"".join(c.data for c in chunks)
    assert text.replace(b"\r", b"") == b"one\ntwo\nthree\n"
    first_seen = {w: next(c.t for c in chunks if w.encode() in c.data) - started for w in ("one", "two", "three")}
    assert first_seen["two"] - first_seen["one"] == pytest.approx(0.2, abs=0.08)
    assert first_seen["three"] - first_seen["two"] == pytest.approx(0.2, abs=0.08)
