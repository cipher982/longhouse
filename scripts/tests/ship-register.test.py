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
    gate = work / "scripts/ci/prepush_gate.py"
    gate.write_text("#!/usr/bin/env python3\nimport os, sys\nprint('GATE-RAN', file=sys.stderr)\nsys.exit(int(os.environ.get('FAKE_GATE_EXIT', '0')))\n")
    (work / "a.txt").write_text("a\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "base")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "origin", "main")
    (work / "a.txt").write_text("b\n")
    git(work, "commit", "-am", "change")
    return bare, work


def run_ship(work: Path, *args: str, session: str | None = "11111111-2222-3333-4444-555555555555", env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("LONGHOUSE_MANAGED_SESSION_ID", "LONGHOUSE_PREPUSH_GATE")}
    env.update(env_extra or {})
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


def test_a_failing_gate_blocks_the_push() -> None:
    with tempfile.TemporaryDirectory() as raw:
        bare, work = make_repo(Path(raw))
        before = git(bare, "rev-parse", "main")
        result = run_ship(work, "--sha", git(work, "rev-parse", "HEAD"), env_extra={"FAKE_GATE_EXIT": "1"})
        assert result.returncode == 4, result.stderr
        assert "GATE-RAN" in result.stderr and "nothing was pushed" in result.stderr
        assert git(bare, "rev-parse", "main") == before
        assert git(bare, "for-each-ref", "refs/ship/") == ""


def test_a_passing_gate_runs_once_before_the_push_and_skip_bypasses_it() -> None:
    with tempfile.TemporaryDirectory() as raw:
        bare, work = make_repo(Path(raw))
        sha = git(work, "rev-parse", "HEAD")
        result = run_ship(work, "--sha", sha)
        assert result.returncode == 0, result.stderr
        assert result.stderr.count("GATE-RAN") == 1
        assert git(bare, "rev-parse", "main") == sha
    with tempfile.TemporaryDirectory() as raw:
        bare, work = make_repo(Path(raw))
        sha = git(work, "rev-parse", "HEAD")
        result = run_ship(work, "--sha", sha, env_extra={"LONGHOUSE_PREPUSH_GATE": "skip", "FAKE_GATE_EXIT": "1"})
        assert result.returncode == 0 and "GATE-RAN" not in result.stderr, result.stderr


def advance_origin(tmp: Path, bare: Path, files: dict[str, str], message: str = "someone else") -> str:
    other = Path(tempfile.mkdtemp(dir=tmp, prefix="other-"))
    shutil.rmtree(other)
    git(tmp, "clone", "-q", str(bare), str(other))
    git(other, "config", "user.email", "o@example.com")
    git(other, "config", "user.name", "o")
    for name, text in files.items():
        (other / name).write_text(text)
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", message)
    git(other, "push", "-q", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def test_a_moved_origin_is_replayed_in_memory_and_the_working_tree_is_untouched() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bare, work = make_repo(tmp)
        mine = git(work, "rev-parse", "HEAD")
        theirs = advance_origin(tmp, bare, {"other.txt": "x\n"})
        status_before = git(work, "status", "--porcelain")
        result = run_ship(work, "--sha", mine)
        assert result.returncode == 0, result.stderr
        landed = git(bare, "rev-parse", "main")
        assert landed not in (mine, theirs)
        assert git(bare, "rev-parse", f"{landed}^") == theirs
        assert git(bare, "log", "-1", "--format=%s", landed) == "change"
        assert git(bare, "show", f"{landed}:a.txt") == "b" and git(bare, "show", f"{landed}:other.txt") == "x"
        assert "replayed" in result.stderr
        assert git(work, "rev-parse", "HEAD") == mine and git(work, "status", "--porcelain") == status_before
        refs = git(bare, "for-each-ref", "--format=%(refname)", "refs/ship/")
        assert landed in refs, "the watch follows the commit that actually landed"


def test_a_real_conflict_stops_the_landing_and_pushes_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bare, work = make_repo(tmp)
        advance_origin(tmp, bare, {"a.txt": "conflicting\n"})
        head = git(bare, "rev-parse", "main")
        result = run_ship(work, "--sha", git(work, "rev-parse", "HEAD"))
        assert result.returncode == 5, result.stderr
        assert "conflicts with origin/main" in result.stderr and "a.txt" in result.stderr
        assert git(bare, "rev-parse", "main") == head and git(bare, "for-each-ref", "refs/ship/") == ""


def test_a_change_someone_already_landed_is_dropped_not_duplicated() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bare, work = make_repo(tmp)
        same = advance_origin(tmp, bare, {"a.txt": "b\n"}, message="same change, other sha")
        advance_origin(tmp, bare, {"z.txt": "z\n"}, message="and more")
        tip = git(bare, "rev-parse", "main")
        result = run_ship(work, "--sha", git(work, "rev-parse", "HEAD"))
        assert result.returncode == 0, result.stderr
        assert git(bare, "rev-parse", "main") == tip, "nothing new to push"
        assert same


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
