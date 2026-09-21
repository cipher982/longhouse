"""close() must reap descendants that outlive the direct child.

start() runs the child through os.setsid(), so the provider leads its own
process group. A provider can leave a descendant in that group after the
direct child exits -- the Cursor Agent leaves a ``node ... worker-server``,
which a qualification run left alive after its artifact root was removed.
Keying the group signal off the direct child's liveness orphaned it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zerg.qa.pty_session import ProviderPtySession


def _group_members(process_group: int) -> list[int]:
    listing = subprocess.run(["ps", "-Ao", "pid=,pgid="], capture_output=True, text=True).stdout
    members = []
    for line in listing.splitlines():
        pid, _, pgid = line.strip().partition(" ")
        if pid.isdigit() and pgid.strip() == str(process_group):
            members.append(int(pid))
    return members


def test_close_reaps_descendants_after_the_direct_child_exits(tmp_path: Path) -> None:
    session = ProviderPtySession.start(
        # The session leader exiting hangs up the PTY, which SIGHUPs the foreground
        # group; ignoring HUP (inherited across exec) is what lets the
        # descendant genuinely outlive the direct child.
        argv=["/bin/sh", "-c", "trap '' HUP; sleep 300 & exit 0"],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        terminal_path=tmp_path / "terminal.raw",
    )
    process_group = session.process.pid
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if session.process.poll() is not None and _group_members(process_group):
                break
            time.sleep(0.1)

        assert session.process.poll() is not None, "fixture's direct child did not exit"
        assert _group_members(process_group), "fixture did not leave a descendant to reap"

        session.close()

        deadline = time.monotonic() + 10
        while _group_members(process_group) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _group_members(process_group), "close() left descendants alive in the owned group"
    finally:
        for pid in _group_members(process_group):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        session.close()
