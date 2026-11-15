#!/usr/bin/env python3
"""Pre-push gate: run the CI lanes a push would trigger, before the push, on the ci VM.

    prepush_gate.py [--base origin/main] [--head HEAD] [--dry-run] [--only lane,lane] [--json]
    prepush_gate.py --hook REMOTE URL            # as git's pre-push hook; ref updates arrive on stdin

What it does, in order:

1. Resolves the committed diff ``base...head`` to lanes with ``scripts/ci/affected.py --gate`` (never the working
   tree: other sessions' staged and untracked files in a shared checkout are not what is being pushed).
2. Exports the exact head commit (``git archive``, no working-tree state) to a clean directory.
3. Runs the cheap local lanes there (rustfmt), then every remote lane at once through ``scripts/ops/remote.sh`` on
   the ``ci`` VM, which has warm test images and never starts a provider CLI. Lanes share the guest's three slots.
4. Prints one line per lane with its time, the log tail of every failed lane, and exits 1 if any failed.

Only pushes to refs/heads/main are gated. ``LONGHOUSE_PREPUSH_GATE=skip`` bypasses it (a revert that must land now);
``LONGHOUSE_PREPUSH_GATE=done:<sha>`` says ship.sh already gated that exact commit. An unreachable ci VM fails the
gate: a push that was not checked says so instead of looking checked.

Control-plane spec: multi-agent-dev-loop-speed.md, change 2.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get("PREPUSH_GATE_WORK", "/tmp/agents/prepush"))
STAGE_NAME = "longhouse-prepush"  # remote.sh keys its warm mirror on the checkout's directory name
ZERO = "0" * 40
LANE_TIMEOUT = int(os.environ.get("PREPUSH_LANE_TIMEOUT", "1800"))
LOCK_WAIT = int(os.environ.get("PREPUSH_LOCK_WAIT", "900"))


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise SystemExit(f"prepush-gate: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def plan(base: str, head: str) -> dict:
    cmd = ["uv", "run", "--no-project", "--with", "pyyaml", "python3", str(ROOT / "scripts/ci/affected.py"),
           "--base", base, "--head", head, "--gate", "--json"]
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(f"prepush-gate: cannot plan lanes: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)


class StageLock:
    """One gate at a time per machine: they share the staged directory and its warm mirror on the ci VM."""

    def __init__(self) -> None:
        self.path = WORK / "stage.lock"

    def __enter__(self) -> "StageLock":
        WORK.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + LOCK_WAIT
        while True:
            try:
                self.path.mkdir()
                (self.path / "pid").write_text(str(os.getpid()))
                return self
            except FileExistsError:
                try:
                    pid = int((self.path / "pid").read_text())
                    os.kill(pid, 0)
                except (OSError, ValueError):
                    shutil.rmtree(self.path, ignore_errors=True)  # holder is gone
                    continue
                if time.time() > deadline:
                    raise SystemExit(f"prepush-gate: another gate (pid {pid}) held {self.path} for over {LOCK_WAIT}s")
                time.sleep(1)

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def export_head(head: str, dest: Path) -> None:
    """Check out exactly ``head`` in a detached worktree of this repository at dest.

    A real checkout, not an archive: remote.sh asks git which files are ignored. It is reused between gates (the
    directory name keys remote.sh's warm mirror on the ci VM), so only the files that differ are rewritten, and
    ``clean -fdx`` removes everything the previous gate left behind. Nothing from the caller's working tree is read.
    """
    if dest.exists() and not (dest / ".git").exists():
        shutil.rmtree(dest)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "prune")
        git("worktree", "add", "--detach", "--force", str(dest), head)
    else:
        git("checkout", "--force", "--detach", head, cwd=dest)
    git("clean", "-fdxq", cwd=dest)
    (dest / ".wt-keep").touch()  # the worktree janitor leaves a checkout that carries this marker


REMOTE_PARALLEL = int(os.environ.get("PREPUSH_REMOTE_PARALLEL", "3"))
# Heaviest first, so the long lanes start at once and the short ones fill the gaps.
LANE_ORDER = ("backend-full", "engine", "managed-launch", "backend-selected", "shipper-e2e", "frontend", "validate",
              "wheel", "provider-contract", "terminal-truth-selfcheck")
DRIVER = """#!/usr/bin/env bash
# Runs every lane in .prepush-lanes/ at most N at a time inside one remote.sh run, and leaves <lane>.log, .rc and .sec in
# $REMOTE_OUT (which remote.sh copies back). One run means one sync and one ssh session; eight parallel remote.sh runs
# overloaded the shared ssh control master ("Session open refused by peer").
set -u
mkdir -p "$REMOTE_OUT"
run_lane() {
  local n="$1" start rc
  start=$(date +%s)
  bash ".prepush-lanes/$n.sh" > "$REMOTE_OUT/$n.log" 2>&1
  rc=$?
  echo "$rc" > "$REMOTE_OUT/$n.rc"
  echo $(( $(date +%s) - start )) > "$REMOTE_OUT/$n.sec"
  echo "lane $n finished: exit $rc in $(( $(date +%s) - start ))s"
}
export -f run_lane
cat .prepush-lanes/order | xargs -P "$PARALLEL" -I{} bash -c 'run_lane {}'
exit 0
"""


def run_local(lane: dict, stage: Path, logdir: Path, out: dict) -> None:
    started = time.time()
    log = logdir / f"{lane['name']}.log"
    with log.open("w") as handle:
        code = subprocess.run(["bash", "-c", lane["command"]], cwd=stage, stdout=handle, stderr=subprocess.STDOUT).returncode
    out[lane["name"]] = {"code": code, "seconds": round(time.time() - started, 1), "log": str(log), "command": lane["command"]}


def run_remote(lanes: list[dict], stage: Path, logdir: Path, out: dict) -> None:
    """Every remote lane through one scripts/ops/remote.sh run on the ci VM, a few at a time."""
    ordered = sorted(lanes, key=lambda l: LANE_ORDER.index(l["name"]) if l["name"] in LANE_ORDER else -1)
    lane_dir = stage / ".prepush-lanes"
    shutil.rmtree(lane_dir, ignore_errors=True)
    lane_dir.mkdir()
    for lane in ordered:
        (lane_dir / f"{lane['name']}.sh").write_text("set -o pipefail\n" + lane["command"] + "\n")
    (lane_dir / "order").write_text("\n".join(l["name"] for l in ordered) + "\n")
    (lane_dir / "driver.sh").write_text(DRIVER)
    timeout = LANE_TIMEOUT
    log = logdir / "remote.log"
    started = time.time()
    with log.open("w") as handle:
        proc = subprocess.Popen(
            [str(stage / "scripts/ops/remote.sh"), "run", "--timeout", str(timeout), "--env", f"PARALLEL={REMOTE_PARALLEL}",
             "bash .prepush-lanes/driver.sh"],
            cwd=stage, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        outdir: str | None = None
        assert proc.stdout is not None
        for line in proc.stdout:
            handle.write(line)
            if line.startswith("lane "):
                print(f"  {line.strip()}", file=sys.stderr)
            if "outputs in " in line:
                outdir = line.rsplit("outputs in ", 1)[1].strip()
        proc.wait()
    elapsed = round(time.time() - started, 1)
    for lane in ordered:
        name = lane["name"]
        rc_file = Path(outdir or "/nonexistent") / "out" / f"{name}.rc"
        lane_log = Path(outdir or "/nonexistent") / "out" / f"{name}.log"
        if rc_file.exists():
            seconds = float((rc_file.with_suffix(".sec")).read_text().strip() or 0)
            out[name] = {"code": int(rc_file.read_text().strip()), "seconds": seconds,
                         "log": str(lane_log), "command": lane["command"]}
        else:  # the lane never reported: the guest was unreachable, timed out or the run was killed
            out[name] = {"code": proc.returncode or 1, "seconds": elapsed, "log": str(log),
                         "command": lane["command"], "infra": True}


def tail(path: str, n: int = 40) -> str:
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(lines[-n:])


def gate(base: str, head: str, *, dry_run: bool, only: set[str] | None, as_json: bool) -> int:
    started = time.time()
    head = git("rev-parse", "--verify", f"{head}^{{commit}}")
    data = plan(base, head)
    lanes = [lane for lane in data["gate"]["lanes"] if not only or lane["name"] in only]
    files = data["files"]
    print(f"prepush-gate: {head[:10]} vs {base}: {len(files)} file(s), {len(lanes)} lane(s)", file=sys.stderr)
    for lane in lanes:
        print(f"  [{lane['where']}] {lane['name']}: {lane['why']}", file=sys.stderr)
    if dry_run or not lanes:
        if as_json:
            print(json.dumps({"head": head, "plan": data["gate"]}))
        return 0

    results: dict[str, dict] = {}
    with StageLock():
        stage = WORK / STAGE_NAME
        export_head(head, stage)
        logdir = WORK / "logs" / head[:12]
        shutil.rmtree(logdir, ignore_errors=True)
        logdir.mkdir(parents=True)
        # Local lanes first (rustfmt takes a second and fails before the guest is touched), then every remote lane at once.
        for lane in [l for l in lanes if l["where"] == "local"]:
            run_local(lane, stage, logdir, results)
        remote = [l for l in lanes if l["where"] == "remote"]
        if remote and not any(r["code"] for r in results.values()):
            run_remote(remote, stage, logdir, results)

    total = round(time.time() - started, 1)
    failed = {name: r for name, r in results.items() if r["code"]}
    for name, r in results.items():
        print(f"  {'FAIL' if r['code'] else 'ok  '} {name:<26} {r['seconds']:>7.1f}s", file=sys.stderr)
    receipt = {"head": head, "base": base, "files": len(files), "total_seconds": total, "lanes": results,
               "ci_only": data["gate"]["ci_only"], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        with (WORK / "receipts.jsonl").open("a") as handle:
            handle.write(json.dumps(receipt) + "\n")
    except OSError:
        pass
    if as_json:
        print(json.dumps(receipt))
    if failed:
        for name, r in failed.items():
            print(f"\n--- {name} failed (exit {r['code']}); reproduce: {r['command']}\n{tail(r['log'])}\n--- full log: {r['log']}", file=sys.stderr)
        print(f"prepush-gate: FAILED in {total}s. Fix and push again. These lanes would have failed CI on main.", file=sys.stderr)
        return 1
    for item in data["gate"]["ci_only"]:
        print(f"  CI only: {item}", file=sys.stderr)
    print(f"prepush-gate: passed in {total}s", file=sys.stderr)
    return 0


def hook(argv: list[str]) -> int:
    mode = os.environ.get("LONGHOUSE_PREPUSH_GATE", "")
    status = 0
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref != "refs/heads/main" or local_sha == ZERO:
            continue
        if mode == "skip":
            print(f"prepush-gate: SKIPPED for {local_sha[:10]} (LONGHOUSE_PREPUSH_GATE=skip); nothing was checked", file=sys.stderr)
            continue
        if mode == f"done:{local_sha}":
            continue
        base = remote_sha if remote_sha != ZERO and subprocess.run(
            ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=ROOT, capture_output=True).returncode == 0 else "origin/main"
        status |= gate(base, local_sha, dry_run=False, only=None, as_json=False)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--dry-run", action="store_true", help="print the lanes and stop")
    parser.add_argument("--only", help="comma list of lane names")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hook", nargs="*", help="run as git's pre-push hook (args: remote name and url)")
    args = parser.parse_args()
    if args.hook is not None:
        return hook(args.hook)
    only = set(args.only.split(",")) if args.only else None
    return gate(args.base, args.head, dry_run=args.dry_run, only=only, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
