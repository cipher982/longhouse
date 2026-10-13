from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

from zerg.qa.live_session_toolkit import PtyProcess
from zerg.qa import pty_reader
from zerg.qa.pty_reader import OwnedPtyReader


def test_close_drains_queued_output_before_finalizing(tmp_path: Path, monkeypatch) -> None:
    started = tmp_path / "provider-started"
    finished = tmp_path / "provider-finished"
    binary = tmp_path / "fake-provider"
    recording = tmp_path / "terminal.tty"
    payload_bytes = 256 * 1024
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "remaining = 256 * 1024\n"
        "chunk = b'x' * 65536\n"
        "while remaining:\n"
        "    written = os.write(1, chunk[:remaining])\n"
        "    remaining -= written\n"
        "pathlib.Path(sys.argv[2]).write_text('finished', encoding='utf-8')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    reader_started = threading.Event()
    allow_reader = threading.Event()
    finalize_signaled = threading.Event()
    original_select = pty_reader.select.select
    original_signal = OwnedPtyReader._signal_reader

    def gated_select(readers, writers, exceptions, timeout=None):
        if threading.current_thread().name.startswith("pty-reader-") and not reader_started.is_set():
            reader_started.set()
            allow_reader.wait()
        return original_select(readers, writers, exceptions, timeout)

    def observe_signal(reader: OwnedPtyReader) -> bool:
        result = original_signal(reader)
        if reader.recording_path == recording:
            finalize_signaled.set()
        return result

    monkeypatch.setattr(pty_reader.select, "select", gated_select)
    monkeypatch.setattr(OwnedPtyReader, "_signal_reader", observe_signal)
    process = PtyProcess(
        [str(binary), str(started), str(finished)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TERM": "xterm-256color"},
        recording=recording,
    )
    close_errors: list[Exception] = []
    closer: threading.Thread | None = None
    try:
        assert reader_started.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        time.sleep(0.05)
        assert not finished.exists(), "fake provider should be blocked on its undrained PTY"

        def close_recording() -> None:
            try:
                process.close()
            except Exception as exc:  # noqa: BLE001 - preserve close failure for the assertion
                close_errors.append(exc)

        closer = threading.Thread(target=close_recording)
        closer.start()
        assert finalize_signaled.wait(timeout=1.0)
        allow_reader.set()
        closer.join(timeout=5.0)
        assert not closer.is_alive()
        assert close_errors == []
        assert finished.exists()
        assert process.process.wait(timeout=2.0) == 0
        assert process.reader_eof_reached is True
        assert recording.stat().st_size >= payload_bytes
    finally:
        allow_reader.set()
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        if closer is not None:
            closer.join(timeout=5.0)
        if not process._closed:
            process.close()
