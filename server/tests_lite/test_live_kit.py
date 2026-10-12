"""The live kit's liveness rule: what "this process has stopped" means for every producer."""

from __future__ import annotations

import os
import subprocess
import sys

from zerg.qa.live_kit import pid_alive
from zerg.qa.live_kit import pid_dead


def test_a_process_that_has_exited_has_stopped_before_its_parent_collects_it():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        # Wait for the exit without collecting it: the entry stays, and a signal to it still succeeds.
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        os.kill(child.pid, 0)
        assert pid_dead(child.pid) and not pid_alive(child.pid)
    finally:
        child.wait()
    assert pid_dead(child.pid)


def test_a_running_process_has_not_stopped_and_a_value_that_is_no_pid_proves_nothing():
    assert pid_alive(os.getpid()) and not pid_dead(os.getpid())
    for no_pid in (None, 0, -1, True, "12"):
        assert not pid_dead(no_pid) and not pid_alive(no_pid)
