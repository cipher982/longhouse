#!/usr/bin/env python3
"""Tell an agent how busy CI is before it pushes or starts waiting.

    make ci-queue                # overview
    make ci-queue SHA=<commit>   # plus: where that commit stands

Facts only, so the agent decides: batch commits instead of pushing each one,
skip waiting and do other work, or wait on a newer commit that already
contains yours. Reads the GitHub API through `gh`; changes nothing.

A queued main commit that is no longer main's head is "superseded": only the
newest verified main commit deploys, and it contains every older one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from collections import defaultdict
from datetime import datetime
from datetime import timezone

REPO = "cipher982/longhouse"
# The push workflows a commit's ship waits on, by workflow file name.
SHIP_WORKFLOWS = ("contract-first-ci.yml", "runtime-image.yml", "deploy-and-verify.yml")


def gh_json(path: str) -> dict:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def age(iso: str | None) -> str:
    if not iso:
        return "?"
    seconds = int((datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds())
    hours, rem = divmod(max(seconds, 0), 3600)
    return f"{hours}h{rem // 60:02d}m" if hours else f"{rem // 60}m"


def hours_since(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600


def runner_pools() -> dict[str, tuple[int, int]]:
    pools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for runner in gh_json(f"repos/{REPO}/actions/runners?per_page=100").get("runners", []):
        if runner.get("status") != "online":
            continue
        # ARC names runners <scale-set>-<hash>-runner-<id>; the scale set is the pool.
        pool = runner["name"].split("-runner-")[0].rsplit("-", 1)[0]
        pools[pool][1] += 1
        pools[pool][0] += int(bool(runner.get("busy")))
    return {pool: (busy, total) for pool, (busy, total) in sorted(pools.items())}


def active_runs() -> list[dict]:
    runs: list[dict] = []
    for status in ("queued", "in_progress"):
        runs += gh_json(f"repos/{REPO}/actions/runs?status={status}&per_page=100").get("workflow_runs", [])
    return runs


def contains(descendant: str, ancestor: str) -> bool:
    """True when `ancestor` is in `descendant`'s history (needs a fetched local clone)."""
    subprocess.run(["git", "fetch", "-q", "origin", "main"], capture_output=True, timeout=30)
    result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], capture_output=True)
    return result.returncode == 0


def workflow_file(run: dict) -> str:
    return str(run.get("path", "")).rsplit("/", 1)[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sha", help="report where this commit stands")
    ap.add_argument("--brief", action="store_true", help="three lines, for printing inside other tools")
    args = ap.parse_args()

    try:
        pools = runner_pools()
        runs = active_runs()
        main_head = gh_json(f"repos/{REPO}/commits/main")["sha"]
    except (subprocess.SubprocessError, KeyError, json.JSONDecodeError) as error:
        print(f"ci-queue: GitHub API unavailable ({error.__class__.__name__}); no queue information", file=sys.stderr)
        return 0

    # A run untouched for a day is abandoned, not waiting; counting it would
    # report a queue measured in months.
    stale = [r for r in runs if hours_since(r["updated_at"]) > 24]
    runs = [r for r in runs if r not in stale]
    queued = [r for r in runs if r["status"] == "queued"]
    running = [r for r in runs if r["status"] == "in_progress"]
    oldest = min((r["created_at"] for r in queued), default=None)
    full = [f"{pool} {busy}/{total}" for pool, (busy, total) in pools.items() if total and busy >= total]
    main_queued = [r for r in queued if r.get("head_branch") == "main" and workflow_file(r) == "contract-first-ci.yml"]
    superseded = sorted({r["head_sha"] for r in main_queued if r["head_sha"] != main_head})
    saturated = bool(full and queued)
    waiting_gates = [r for r in running if workflow_file(r) == "deploy-and-verify.yml"]

    headline = (
        f"CI {'SATURATED' if saturated else 'ok'}: {len(running)} running, {len(queued)} queued"
        + (f", oldest waiting {age(oldest)}" if oldest else "")
        + (f"; full pools: {', '.join(full)}" if full else "")
    )
    lines = [headline]
    if superseded:
        lines.append(
            f"{len(superseded)} queued main CI run(s) are for commits already behind main head {main_head[:9]}; "
            "only the newest verified main commit deploys, and it contains them."
        )

    if args.sha:
        sha = args.sha
        mine = [r for r in runs if r["head_sha"].startswith(sha)]
        if mine:
            for run in sorted(mine, key=lambda r: r["created_at"]):
                position = ""
                if run["status"] == "queued":
                    ahead = sum(1 for q in queued if q["created_at"] < run["created_at"])
                    position = f", {ahead} run(s) queued ahead"
                lines.append(f"  {sha[:9]} {run['name']}: {run['status']} for {age(run['created_at'])}{position}")
        else:
            lines.append(f"  {sha[:9]}: no queued or running workflow runs (finished, or not triggered by its paths)")
        if not main_head.startswith(sha) and contains(main_head, sha):
            head_ci = next((r for r in runs if r["head_sha"] == main_head and workflow_file(r) == "contract-first-ci.yml"), None)
            state = f"CI {head_ci['status']}" if head_ci else "CI finished or not yet listed"
            lines.append(f"  covered: main head {main_head[:9]} contains {sha[:9]} ({state}); its result and deploy include your change.")

    if args.brief:
        print("\n".join(lines[:3]))
        return 0

    lines.append("Runner pools (busy/online): " + ", ".join(f"{p} {b}/{t}" for p, (b, t) in pools.items()))
    if waiting_gates:
        lines.append(f"{len(waiting_gates)} Deploy and Verify run(s) hold deploy runners while they wait for their CI.")
    if stale:
        lines.append(
            f"Ignored {len(stale)} abandoned run(s) untouched for over a day: "
            + ", ".join(f"{r['name']} {r['head_sha'][:9]} ({age(r['created_at'])} old, run {r['id']})" for r in stale[:3])
        )
    by_workflow = Counter(f"{r['name']} [{r['status']}]" for r in runs)
    lines.append("Active runs: " + ", ".join(f"{k} x{v}" for k, v in sorted(by_workflow.items())))
    if saturated:
        lines.append(
            "Options: batch further commits into one push; do other work instead of blocking on this queue; "
            "or wait on the newest main commit that contains yours."
        )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
