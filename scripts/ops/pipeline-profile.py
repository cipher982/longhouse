#!/usr/bin/env python3
"""Windowed wall-time profile of the build, test, release and deploy pipeline.

`ci-profile.py` explains one run; this answers "where does the pipeline spend
its time and its compute over the last N days", from GitHub's API only:

  * per main push: push -> CI -> image -> canary -> Launch Gate, median/p90/max
  * per job: duration, queue time (`started_at - created_at`), failures, runner
    label, sorted by total runner time
  * per step for the slowest jobs

Read-only. Needs `gh` auth. Use `--repo cipher982/longhouse-control-plane` for
the private repo (workflow names differ, the ship-path section is skipped).
Compare two eras with `--since`. Full method and the 2026-09-29 numbers:
control-plane docs/specs/build-compute-pipeline.md.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures as futures
import json
import math
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

SHIP_WORKFLOWS = ("CI", "Publish Runtime Image", "Deploy and Verify", "Launch Gate")


def gh(path: str) -> list[dict]:
    out = subprocess.run(
        ["gh", "api", path, "--paginate", "--jq", ".workflow_runs // .jobs"],
        capture_output=True, text=True, timeout=180, check=True,
    ).stdout
    rows: list[dict] = []
    decoder = json.JSONDecoder()
    text = out.strip()
    while text:
        chunk, end = decoder.raw_decode(text)
        rows.extend(chunk)
        text = text[end:].lstrip()
    return rows


def ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def minutes(a: str | None, b: str | None) -> float | None:
    start, end = ts(a), ts(b)
    return (end - start).total_seconds() / 60 if start and end else None


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def summary(values: list[float]) -> str:
    return f"med {statistics.median(values):5.1f}  p90 {pct(values, .9):5.1f}  max {max(values):5.1f}"


def fetch_runs(repo: str, first: date, last: date) -> list[dict]:
    # The API caps a query at 1000 rows: fetch one created-day at a time.
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    with futures.ThreadPoolExecutor(6) as pool:
        chunks = pool.map(lambda d: gh(f"repos/{repo}/actions/runs?created={d}&per_page=100"), days)
    return [run for chunk in chunks for run in chunk if run["status"] == "completed"]


def fetch_jobs(repo: str, run_ids: list[int]) -> dict[int, list[dict]]:
    with futures.ThreadPoolExecutor(8) as pool:
        results = pool.map(lambda i: gh(f"repos/{repo}/actions/runs/{i}/jobs?per_page=100"), run_ids)
        return dict(zip(run_ids, results))


def ship_path(runs: list[dict]) -> None:
    by_sha: dict[str, dict[str, list[dict]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for run in runs:
        if run["event"] == "push" and run["head_branch"] == "main" and run["name"] in SHIP_WORKFLOWS:
            by_sha[run["head_sha"]][run["name"]].append(run)
    stages: dict[str, list[float]] = collections.defaultdict(list)
    shas = 0
    for workflows in by_sha.values():
        if "CI" not in workflows or "Deploy and Verify" not in workflows:
            continue
        origin = min(r["created_at"] for r in workflows["CI"])
        done = {}
        for name, items in workflows.items():
            ok = [r["updated_at"] for r in items if r["conclusion"] == "success"]
            if ok:
                done[name] = min(ok)
        if "CI" not in done or "Deploy and Verify" not in done:
            continue
        shas += 1
        for name, end in done.items():
            stages[name].append(minutes(origin, end) or 0)
    print(f"\n== Main push to stage complete (minutes), {shas} SHAs with green CI and Deploy and Verify")
    for name in SHIP_WORKFLOWS:
        if stages[name]:
            print(f"  {name:24} n={len(stages[name]):3}  {summary(stages[name])}")


def job_table(runs: list[dict], jobs: dict[int, list[dict]], top: int, steps_for: int) -> None:
    names = {run["id"]: run["name"] for run in runs}
    rows: dict[tuple[str, str], list[tuple[float, float, str, str]]] = collections.defaultdict(list)
    step_rows: dict[tuple[str, str, str], list[float]] = collections.defaultdict(list)
    for run_id, run_jobs in jobs.items():
        for job in run_jobs:
            if job["conclusion"] not in ("success", "failure") or not job["started_at"]:
                continue
            duration = minutes(job["started_at"], job["completed_at"])
            queue = minutes(job["created_at"], job["started_at"])
            if duration is None or queue is None:
                continue
            key = (names[run_id], job["name"])
            label = ",".join(job.get("labels") or [])
            rows[key].append((duration, queue, job["conclusion"], label))
            if job["conclusion"] == "success":
                for step in job.get("steps") or []:
                    if step["conclusion"] == "success":
                        d = minutes(step["started_at"], step["completed_at"])
                        if d is not None:
                            step_rows[(*key, step["name"])].append(d)
    ranked = sorted(rows.items(), key=lambda kv: -sum(r[0] for r in kv[1]))
    print(f"\n== Jobs by total runner time (durations and queue in minutes; queue = created -> started)")
    print(f"  {'workflow / job':64} {'n':>4} {'fail':>4} {'dur med':>8} {'p90':>6} {'q med':>6} {'q p90':>6} {'hours':>6}  runner")
    for (workflow, job), items in ranked[:top]:
        d = [i[0] for i in items]
        q = [i[1] for i in items]
        fails = sum(1 for i in items if i[2] == "failure")
        print(
            f"  {(workflow[:20] + ' / ' + job)[:64]:64} {len(items):4} {fails:4} {statistics.median(d):8.1f} "
            f"{pct(d, .9):6.1f} {statistics.median(q):6.1f} {pct(q, .9):6.1f} {sum(d) / 60:6.1f}  {items[0][3]}"
        )
    print(f"\n== Steps of the {steps_for} slowest jobs (successful runs; minutes)")
    slow = {key for key, _ in ranked[:steps_for]}
    hot = sorted(
        ((sum(v), k, v) for k, v in step_rows.items() if k[:2] in slow), key=lambda x: -x[0]
    )[: steps_for * 2]
    for total, (workflow, job, step), values in hot:
        print(f"  {(workflow[:14] + ' / ' + job[:28] + ' / ' + step)[:84]:84} n={len(values):3} {summary(values)}  total {total / 60:5.1f}h")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default="cipher982/longhouse")
    parser.add_argument("--days", type=int, default=7, help="window length ending today (UTC)")
    parser.add_argument("--since", help="first day, YYYY-MM-DD (overrides --days)")
    parser.add_argument("--until", help="last day, YYYY-MM-DD (default today)")
    parser.add_argument("--top", type=int, default=25, help="rows in the job table")
    parser.add_argument("--steps-for", type=int, default=6, help="show steps for this many slowest jobs")
    args = parser.parse_args()

    last = date.fromisoformat(args.until) if args.until else datetime.now(timezone.utc).date()
    first = date.fromisoformat(args.since) if args.since else last - timedelta(days=args.days - 1)
    runs = fetch_runs(args.repo, first, last)
    print(f"{args.repo}: {len(runs)} completed runs, {first} to {last}", file=sys.stderr)
    if args.repo.endswith("/longhouse"):
        ship_path(runs)
    jobs = fetch_jobs(args.repo, [run["id"] for run in runs])
    job_table(runs, jobs, args.top, args.steps_for)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
