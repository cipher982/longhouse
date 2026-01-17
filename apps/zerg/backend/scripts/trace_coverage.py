#!/usr/bin/env python3
"""Generate a trace coverage report for observability regression checks.

Usage:
    uv run python scripts/trace_coverage.py
    uv run python scripts/trace_coverage.py --since-hours 24
    uv run python scripts/trace_coverage.py --since-days 7 --json
    uv run python scripts/trace_coverage.py --min-percent 95
    uv run python scripts/trace_coverage.py --min-percent 95 --min-event-percent 90
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.database import get_session_factory
from zerg.services.trace_coverage import build_trace_coverage_report


def _format_bucket(bucket: dict) -> str:
    if bucket.get("empty"):
        pct = "n/a"
    else:
        pct = f"{bucket['pct']:.2f}%"
    return f"{bucket['name']}: {bucket['with_trace']}/{bucket['total']} ({pct})"


def _failures(buckets: list[dict], min_percent: float) -> list[dict]:
    return [
        bucket
        for bucket in buckets
        if not bucket.get("empty") and bucket.get("pct", 0.0) < min_percent
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace coverage report")
    parser.add_argument("--since-hours", type=int, default=None, help="Only include rows newer than N hours")
    parser.add_argument("--since-days", type=int, default=None, help="Only include rows newer than N days")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--min-percent", type=float, default=None, help="Fail if any core bucket below threshold")
    parser.add_argument("--min-event-percent", type=float, default=None, help="Fail if any event type below threshold")
    parser.add_argument("--no-events", action="store_true", help="Skip per-event-type breakdown")
    args = parser.parse_args()

    since = None
    if args.since_hours is not None or args.since_days is not None:
        total_hours = (args.since_days or 0) * 24 + (args.since_hours or 0)
        since = datetime.now(timezone.utc) - timedelta(hours=total_hours)

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        report = build_trace_coverage_report(db, since=since)
    finally:
        db.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("Trace Coverage Report")
        if report.get("since"):
            print(f"Window: {report['since']} -> {report['generated_at']}")
        else:
            print(f"Generated: {report['generated_at']}")
        print("")
        print("Core Buckets")
        for bucket in report["buckets"]:
            print(f"- {_format_bucket(bucket)}")

        if not args.no_events:
            print("")
            print("Event Types")
            for bucket in report["event_types"]:
                print(f"- {_format_bucket(bucket)}")

    if args.min_percent is not None:
        failures = _failures(report["buckets"], args.min_percent)
        if failures:
            print("\nFAIL: core trace coverage below threshold")
            for bucket in failures:
                print(f"- {_format_bucket(bucket)}")
            return 2

    if args.min_event_percent is not None and not args.no_events:
        failures = _failures(report["event_types"], args.min_event_percent)
        if failures:
            print("\nFAIL: event trace coverage below threshold")
            for bucket in failures:
                print(f"- {_format_bucket(bucket)}")
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
