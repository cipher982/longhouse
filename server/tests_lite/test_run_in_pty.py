"""Regression tests for the shared bounded PTY command wrapper."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "run-in-pty.py"


def test_timeout_kills_owned_process_group_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-survived"
    descendant_code = (
        "import pathlib, sys, time; "
        "time.sleep(1); pathlib.Path(sys.argv[1]).write_text('alive')"
    )
    leader_code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "time.sleep(30)"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--timeout",
            "0.2",
            sys.executable,
            "-c",
            leader_code,
            descendant_code,
            str(marker),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 124, result.stderr
    time.sleep(1.2)
    assert not marker.exists(), result.stdout


def test_timeout_drain_has_an_absolute_bound() -> None:
    chatty_code = (
        "import os, time; "
        "data = b'x' * 4096; "
        "while True: os.write(1, data); time.sleep(0.01)"
    )
    leader_code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1]]); "
        "time.sleep(30)"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--timeout",
            "0.2",
            sys.executable,
            "-c",
            leader_code,
            chatty_code,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 124, result.stderr
