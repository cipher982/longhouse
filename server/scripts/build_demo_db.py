#!/usr/bin/env python3
"""Build a demo SQLite database with seeded Longhouse demo sessions.

Usage:
  uv run python server/scripts/build_demo_db.py
  uv run python server/scripts/build_demo_db.py --output /path/to/demo.db
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

# Add backend to path when invoked as a repository script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.services.demo_database import build_demo_database


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    default_output = repo_root / "data" / "demo" / "longhouse-demo.db"

    parser = argparse.ArgumentParser(description="Build a demo SQLite database")
    parser.add_argument("--output", default=str(default_output), help="Output SQLite file path")
    parser.add_argument("--owner-email", default="local@zerg", help="Owner email for seeded runs")
    parser.add_argument("--force", action="store_true", help="Overwrite existing DB if present")
    parser.add_argument(
        "--anchor",
        default=None,
        help=(
            "ISO timestamp to anchor seeded session times to (e.g. 2026-01-15T09:41:00). "
            "Fixing this makes captures byte-stable run-to-run; defaults to now."
        ),
    )

    args = parser.parse_args()
    output_path = Path(args.output).expanduser().resolve()

    anchor: datetime | None = None
    if args.anchor:
        anchor = datetime.fromisoformat(args.anchor)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        if not args.force:
            print(f"ERROR: Demo DB already exists: {output_path}")
            print("Re-run with --force to overwrite.")
            return 2
        output_path.unlink()

    paths = build_demo_database(output_path, owner_email=args.owner_email, anchor=anchor)
    print(
        f"Demo corpus created: {paths['legacy']} (legacy), {paths['live']} (storage-v2), "
        f"{paths['search']} (searchd)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
