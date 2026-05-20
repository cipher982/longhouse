#!/usr/bin/env python3
"""Compute per-stage latency percentiles from flight-recorder ship_trace events.

Usage:
  scripts/ops/flight-recorder-percentiles.py [path-or-glob] [--since ISO8601]

Defaults to today's flight recorder file under ~/.longhouse/agent/flight-recorder/.
Use this between simplification iterations to verify the bottleneck stage moved
(or didn't). Pairs with docs/specs/transcript-hot-plane-simplification.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys
from collections import defaultdict

STAGES = [
    "observation_window_ms",
    "observation_to_enqueue_ms",
    "wake_to_enqueue_ms",
    "enqueue_to_job_ms",
    "prepare_blocking_queue_wait_ms",
    "prepare_binding_wait_ms",
    "prepare_open_db_ms",
    "prepare_identity_ms",
    "prepare_parse_ms",
    "prepare_batch_build_ms",
    "prepare_ms",
    "job_to_http_ms",
    "http_latency_ms",
    "observed_to_job_ms",
]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def fmt(v: float | None) -> str:
    if v is None:
        return "    -"
    return f"{int(round(v)):>5}"


def load_traces(paths: list[str], since: dt.datetime | None) -> list[dict]:
    rows = []
    for path in paths:
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("schema") != "ship_trace.v1":
                    continue
                if since is not None:
                    rec = d.get("recorded_at")
                    if rec:
                        try:
                            ts = dt.datetime.fromisoformat(rec.replace("Z", "+00:00"))
                            if ts < since:
                                continue
                        except ValueError:
                            pass
                rows.append(d)
    return rows


def render_table(label: str, rows: list[dict]) -> None:
    print(f"\n=== {label}  (n={len(rows)}) ===")
    if not rows:
        return
    print(f"{'stage':<35}  {'p50':>5}  {'p95':>5}  {'p99':>5}  {'max':>6}")
    print(f"{'-'*35}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*6}")
    for stage in STAGES:
        vals = [r[stage] for r in rows if isinstance(r.get(stage), (int, float))]
        if not vals:
            continue
        p50 = percentile(vals, 50)
        p95 = percentile(vals, 95)
        p99 = percentile(vals, 99)
        m = max(vals)
        print(f"{stage:<35}  {fmt(p50)}  {fmt(p95)}  {fmt(p99)}  {int(round(m)):>6}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--since", help="ISO8601 timestamp; only rows recorded after this")
    ap.add_argument("--by-source", action="store_true", help="Break out by observation_source")
    ap.add_argument("--by-context", action="store_true", help="Break out by work_context")
    args = ap.parse_args()

    if args.path is None:
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        default_path = os.path.expanduser(
            f"~/.longhouse/agent/flight-recorder/flight-{today}.jsonl"
        )
        paths = [default_path] if os.path.exists(default_path) else []
    elif any(c in args.path for c in "*?["):
        paths = sorted(glob.glob(args.path))
    elif os.path.isdir(args.path):
        paths = sorted(glob.glob(os.path.join(args.path, "flight-*.jsonl")))
    else:
        paths = [args.path]

    if not paths:
        print("no flight recorder files found", file=sys.stderr)
        return 1

    since = None
    if args.since:
        since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt.timezone.utc)

    rows = load_traces(paths, since)
    print(f"loaded {len(rows)} ship_trace.v1 events from {len(paths)} file(s)")

    render_table("ALL", rows)

    if args.by_source:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups[r.get("observation_source", "unknown")].append(r)
        for src, sub in sorted(groups.items()):
            render_table(f"observation_source = {src}", sub)

    if args.by_context:
        groups2: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups2[r.get("work_context", "unknown")].append(r)
        for ctx, sub in sorted(groups2.items()):
            render_table(f"work_context = {ctx}", sub)

    return 0


if __name__ == "__main__":
    sys.exit(main())
