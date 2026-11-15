#!/usr/bin/env python3
"""make ship pushes and returns; the push registers the ship-watch ref (ship.sh)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def make_repo(tmp: Path) -> tuple[Path, Path]:
    bare = tmp / "origin.git"
    git(tmp, "init", "--bare", "-b", "main", str(bare))
    work = tmp / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    (work / "scripts" / "ops").mkdir(parents=True)
    (work / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/ops/ship.sh", work / "scripts/ops/ship.sh")
    (work / "scripts/ci/verifier_boundary.py").write_text("#!/usr/bin/env python3\n")
    ci_queue = work / "scripts/ops/ci-queue.py"
    ci_queue.write_text("#!/bin/sh\nexit 0\n")
    ci_queue.chmod(0o755)
    monitor = work / "scripts/ops/ship-monitor.py"
    monitor.write_text("#!/bin/sh\necho MONITOR-RAN\n")
    monitor.chmod(0o755)
    (work / "a.txt").write_text("a\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "base")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "origin", "main")
    (work / "a.txt").write_text("b\n")
    git(work, "commit", "-am", "change")
    return bare, work


def run_ship(work: Path, *args: str, session: str | None = "11111111-2222-3333-4444-555555555555") -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "LONGHOUSE_MANAGED_SESSION_ID"}
    if session:
        env["LONGHOUSE_MANAGED_SESSION_ID"] = session
    return subprocess.run(
        ["bash", str(work / "scripts/ops/ship.sh"), "--branch", "main", *args],
        cwd=work, env=env, text=True, capture_output=True, timeout=60,
    )


def test_push_registers_the_watch_ref_and_returns_without_waiting() -> None:
    with tempfile.TemporaryDirectory() as raw:
        bare, work = make_repo(Path(raw))
        sha = git(work, "rev-parse", "HEAD")
        result = run_ship(work, "--sha", sha, "--deadline-min", "30")
        assert result.returncode == 0, result.stderr
        assert "MONITOR-RAN" not in result.stdout
        assert git(bare, "rev-parse", "main") == sha
        refs = git(bare, "for-each-ref", "--format=%(refname)", "refs/ship/").splitlines()
        assert len(refs) == 1
        _, _, ref_sha, session, pushed, deadline = refs[0].split("/")
        assert (ref_sha, session) == (sha, "11111111-2222-3333-4444-555555555555")
        assert int(deadline) - int(pushed) == 30 * 60


def test_no_session_registers_none() -> None:
    with tempfile.TemporaryDirectory() as raw:
        bare, work = make_repo(Path(raw))
        result = run_ship(work, "--sha", git(work, "rev-parse", "HEAD"), session=None)
        assert result.returncode == 0, result.stderr
        assert git(bare, "for-each-ref", "--format=%(refname)", "refs/ship/").split("/")[3] == "none"


def test_wait_keeps_the_blocking_monitor() -> None:
    with tempfile.TemporaryDirectory() as raw:
        bare, work = make_repo(Path(raw))
        result = run_ship(work, "--sha", git(work, "rev-parse", "HEAD"), "--wait")
        assert result.returncode == 0, result.stderr
        assert "MONITOR-RAN" in result.stdout
        assert git(bare, "for-each-ref", "refs/ship/") == ""


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
