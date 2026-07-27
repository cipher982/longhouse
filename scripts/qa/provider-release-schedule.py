#!/usr/bin/env python3
"""Validate the provider schedule, emit its matrix, or check build staleness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER))

from zerg.qa.provider_release_schedule import DEFAULT_SCHEDULE_PATH  # noqa: E402
from zerg.qa.provider_release_schedule import ProviderReleaseScheduleError  # noqa: E402
from zerg.qa.provider_release_schedule import build_store_staleness  # noqa: E402
from zerg.qa.provider_release_schedule import load_provider_release_schedule  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("matrix")
    staleness_parser = subparsers.add_parser("staleness")
    staleness_parser.add_argument("--store-root", required=True, type=Path)
    staleness_parser.add_argument("--provider", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        schedule = load_provider_release_schedule(args.schedule)
        payload = (
            schedule.matrix()
            if args.command == "matrix"
            else build_store_staleness(
                store_root=args.store_root,
                schedule=schedule,
                providers=set(args.provider) if args.provider else None,
            )
        )
    except ProviderReleaseScheduleError as exc:
        parser.error(str(exc))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if args.command == "staleness" and payload["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
