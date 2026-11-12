#!/usr/bin/env python3
"""What CI cost over a window: runs per push, runner minutes, queue waits.

`ci-queue.py` says how busy CI is now. This says what a period of pushes cost,
so a change to the workflows can be judged by the same numbers before and after:

    scripts/ops/ci-cost.py --since 2026-11-11T00:00:00Z --until 2026-11-12T20:38:00Z
    scripts/ops/ci-cost.py --since 2026-11-12T20:38:00Z

Counts only jobs a runner picked up. A job that was queued with no runner and
then cancelled has no runner time and is listed as "never ran". Reads the
GitHub API through `gh` and changes nothing. Job lists of completed runs are
cached under .build/ci-cost/, so a second report over the same window is quick.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import Counter
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

REPO = "cipher982/longhouse"
CACHE = Path(__file__).resolve().parents[2] / ".build" / "ci-cost"
LONG_WAIT_MINUTES = 2.0


def when(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_json(path: str) -> dict:
    last = ""
    for _ in range(3):
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            return json.loads(out.stdout)
        last = out.stderr.strip()
    raise RuntimeError(f"gh api {path}: {last}")


def runs_between(start: datetime, end: datetime) -> list[dict]:
    """Every workflow run created in the window. The API returns at most 1000 per query, so ask in 6-hour slices."""
    runs: dict[int, dict] = {}
    cursor = start
    while cursor < end:
        upper = min(cursor + timedelta(hours=6), end)
        page = 1
        while True:
            query = f"repos/{REPO}/actions/runs?created={stamp(cursor)}..{stamp(upper)}&per_page=100&page={page}"
            batch = gh_json(query).get("workflow_runs", [])
            for run in batch:
                runs[run["id"]] = run
            if len(batch) < 100:
                break
            page += 1
        cursor = upper
    return [run for run in runs.values() if start <= when(run["created_at"]) < end]


def jobs_of(run: dict) -> list[dict]:
    cached = CACHE / f"{run['id']}-{run.get('run_attempt', 1)}.json"
    if cached.is_file():
        return json.loads(cached.read_text())
    jobs = gh_json(f"repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=100&filter=latest").get("jobs", [])
    if run.get("status") == "completed":
        CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(jobs))
    return jobs


def quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def share_at_peak(spans: list[tuple[datetime, datetime]], window_seconds: float) -> tuple[int, float]:
    """(most jobs running at once, share of the window spent at that number)."""
    events = sorted([(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans])
    peak = running = 0
    for _, step in events:
        running += step
        peak = max(peak, running)
    at_peak = 0.0
    running = 0
    previous = None
    for moment, step in events:
        if previous is not None and running == peak:
            at_peak += (moment - previous).total_seconds()
        running += step
        previous = moment
    return peak, (at_peak / window_seconds if window_seconds else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="window start, ISO 8601 UTC")
    ap.add_argument("--until", help="window end, ISO 8601 UTC (default: now)")
    ap.add_argument("--jobs", action="store_true", help="also list every job: runs, median minutes, median wait, total")
    args = ap.parse_args()

    start = when(args.since)
    end = when(args.until) if args.until else datetime.now(timezone.utc)
    days = (end - start).total_seconds() / 86400
    runs = runs_between(start, end)
    with ThreadPoolExecutor(max_workers=8) as pool:
        job_lists = list(pool.map(jobs_of, runs))

    pushes = {run["head_sha"] for run in runs if run["event"] == "push" and run.get("head_branch") == "main"}
    per_push = Counter(run["head_sha"] for run in runs if run["head_sha"] in pushes)
    by_workflow: dict[str, Counter] = defaultdict(Counter)
    for run in runs:
        by_workflow[run["name"]][run["event"]] += 1

    print(f"window {stamp(start)} to {stamp(end)} ({days:.2f} days), {len(runs)} runs")
    print(f"pushes to main: {len(pushes)} ({len(pushes) / days:.0f} a day)")
    if per_push:
        counts = list(per_push.values())
        print(f"runs per push: median {statistics.median(counts):.0f}, mean {statistics.fmean(counts):.1f}")

    print("\nruns by workflow (all triggers), and how many pushes started it:")
    for name, events in sorted(by_workflow.items(), key=lambda item: -sum(item[1].values())):
        detail = ", ".join(f"{event} {count}" for event, count in events.most_common())
        started_by = len({run["head_sha"] for run in runs if run["name"] == name and run["event"] == "push" and run["head_sha"] in pushes})
        print(f"  {name}: {sum(events.values())} ({detail}); on {started_by} of {len(pushes)} pushes")

    ci_walls = [
        (when(run["updated_at"]) - when(run["created_at"])).total_seconds() / 60
        for run in runs
        if run["path"].endswith("contract-first-ci.yml") and run["event"] == "push" and run.get("conclusion") in ("success", "failure")
    ]
    if ci_walls:
        print(f"\nCI run, created to completed: median {statistics.median(ci_walls):.1f} min, p90 {quantile(ci_walls, 0.9):.1f} min ({len(ci_walls)} runs)")
    ci_jobs_ran = []

    pools: dict[str, dict] = defaultdict(lambda: {"minutes": 0.0, "waits": [], "spans": [], "never_ran": 0})
    per_job: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"minutes": [], "waits": [], "skipped": 0})
    for run, jobs in zip(runs, job_lists):
        ran_here = 0
        for job in jobs:
            label = ",".join(job.get("labels") or []) or "?"
            key = (run["name"], job["name"], label)
            if job.get("conclusion") == "skipped":
                per_job[key]["skipped"] += 1
                continue
            if not (job.get("runner_name") and job.get("started_at") and job.get("completed_at")):
                pools[label]["never_ran"] += 1
                continue
            begun, done = when(job["started_at"]), when(job["completed_at"])
            minutes = (done - begun).total_seconds() / 60
            wait = max((begun - when(job["created_at"])).total_seconds() / 60, 0.0)
            pools[label]["minutes"] += minutes
            pools[label]["waits"].append(wait)
            pools[label]["spans"].append((begun, done))
            per_job[key]["minutes"].append(minutes)
            per_job[key]["waits"].append(wait)
            ran_here += 1
        if run["path"].endswith("contract-first-ci.yml") and run["event"] == "push" and run.get("conclusion") in ("success", "failure"):
            ci_jobs_ran.append(ran_here)
    if ci_jobs_ran:
        print(f"CI jobs that took a runner, per push run: median {statistics.median(ci_jobs_ran):.0f}, mean {statistics.fmean(ci_jobs_ran):.1f}")

    print("\nrunner pools (jobs a runner picked up):")
    for label, pool_stats in sorted(pools.items(), key=lambda item: -item[1]["minutes"]):
        waits = pool_stats["waits"]
        if not waits:
            continue
        peak, share = share_at_peak(pool_stats["spans"], (end - start).total_seconds())
        long_waits = sum(1 for wait in waits if wait > LONG_WAIT_MINUTES) / len(waits)
        print(
            f"  {label}: {len(waits)} jobs, {pool_stats['minutes']:.0f} runner-minutes ({pool_stats['minutes'] / days:.0f} a day); "
            f"wait median {statistics.median(waits):.1f} / p90 {quantile(waits, 0.9):.1f} / max {max(waits):.0f} min, "
            f"{long_waits:.0%} waited over {LONG_WAIT_MINUTES:.0f} min; peak {peak} at once for {share:.1%} of the window; "
            f"never ran {pool_stats['never_ran']}"
        )

    if args.jobs:
        print("\njobs: workflow | job | pool | ran | skipped | median min | median wait | total min")
        for (workflow, name, label), job_stats in sorted(per_job.items(), key=lambda item: -sum(item[1]["minutes"])):
            minutes, waits = job_stats["minutes"], job_stats["waits"]
            print(
                f"  {workflow} | {name} | {label} | {len(minutes)} | {job_stats['skipped']} | "
                f"{statistics.median(minutes) if minutes else 0:.1f} | {statistics.median(waits) if waits else 0:.1f} | {sum(minutes):.0f}"
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ci-cost: {error}", file=sys.stderr)
        raise SystemExit(1)
