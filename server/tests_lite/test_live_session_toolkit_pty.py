from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

from zerg.qa import pty_reader
from zerg.qa.live_session_toolkit import PtyProcess
from zerg.qa.live_session_toolkit import RuntimeHostRegistrationTransient
from zerg.qa.live_session_toolkit import wait_state
from zerg.qa.provider_native_resume import SPECS


def test_pty_reader_drains_and_bounds_output_while_waiter_polls_marker(tmp_path: Path) -> None:
    marker = tmp_path / "provider-finished"
    release = tmp_path / "provider-release"
    binary = tmp_path / "fake-provider"
    recording = tmp_path / "terminal.tty"
    payload_bytes = 256 * 1024
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        "for byte in (b'a', b'z'):\n"
        "    remaining = 128 * 1024\n"
        "    chunk = byte * 65536\n"
        "    while remaining:\n"
        "        written = os.write(1, chunk[:remaining])\n"
        "        remaining -= written\n"
        "pathlib.Path(sys.argv[1]).write_text('complete', encoding='utf-8')\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    process = PtyProcess(
        [str(binary), str(marker), str(release)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TERM": "xterm-256color"},
        recording=recording,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "provider blocked writing its PTY before publishing the marker"

        deadline = time.monotonic() + 3.0
        while process.dropped_output_bytes < payload_bytes - 64 * 1024 and time.monotonic() < deadline:
            time.sleep(0.01)
        dropped = process.dropped_output_bytes
        buffered = process.drain()
        assert len(buffered) <= 64 * 1024
        assert buffered == b"z" * (64 * 1024)
        assert dropped == payload_bytes - len(buffered)

        release.write_text("release", encoding="utf-8")
        assert process.wait(timeout=2.0) == 0
    finally:
        release.write_text("release", encoding="utf-8")
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        process.close()

    assert recording.stat().st_size >= payload_bytes


def test_wait_state_waits_for_final_registration_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    binary = tmp_path / "fake-provider"
    recording = tmp_path / "terminal.tty"
    diagnostic = "managed Claude resume launch failed: Runtime Host returned HTTP 503 Service Unavailable\n"
    binary.write_text(
        f"#!/usr/bin/env python3\nimport os\nos.write(1, {diagnostic.encode()!r})\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    reader_started = threading.Event()
    allow_reader = threading.Event()
    captured_bytes: list[bytes] = []
    original_read = pty_reader.os.read

    def gated_read(fd: int, size: int) -> bytes:
        captured = original_read(fd, size)
        if captured and threading.current_thread().name.startswith("pty-reader-") and not reader_started.is_set():
            captured_bytes.append(captured)
            reader_started.set()
            allow_reader.wait()
        return captured

    monkeypatch.setattr(pty_reader.os, "read", gated_read)
    process = PtyProcess(
        [str(binary)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TERM": "xterm-256color"},
        recording=recording,
    )
    outcome: list[object] = []
    waiter: threading.Thread | None = None
    try:
        assert reader_started.wait(timeout=3.5)
        assert diagnostic.strip().encode() in b"".join(captured_bytes)
        assert process.process.wait(timeout=2.0) == 0

        def observe_state() -> None:
            try:
                outcome.append(wait_state(SPECS["claude"], home, process=process, timeout=2.0))
            except Exception as exc:  # noqa: BLE001 - preserve the exact exit classification
                outcome.append(exc)

        waiter = threading.Thread(target=observe_state)
        waiter.start()
        time.sleep(0.7)
        assert waiter.is_alive(), "wait_state classified process exit before PTY EOF"

        allow_reader.set()
        waiter.join(timeout=3.0)
        assert not waiter.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], RuntimeHostRegistrationTransient)
        assert diagnostic.strip() in recording.read_text(encoding="utf-8")
    finally:
        allow_reader.set()
        if waiter is not None:
            waiter.join(timeout=3.0)
        if process.process.poll() is None:
            process.kill_group(signal.SIGKILL)
            process.process.wait(timeout=2.0)
        process.close()
