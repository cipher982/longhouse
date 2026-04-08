#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import zipfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate built Longhouse wheel archives.")
    parser.add_argument("wheel", type=Path, nargs="+", help="Wheel archive(s) to validate")
    return parser.parse_args()


def validate_wheel(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"wheel not found: {path}")

    with zipfile.ZipFile(path) as archive:
        counts = collections.Counter(archive.namelist())

    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        joined = "\n".join(f"  - {name}" for name in duplicates)
        raise SystemExit(f"{path} contains duplicate ZIP entries:\n{joined}")


def main() -> int:
    args = parse_args()
    for wheel in args.wheel:
        validate_wheel(wheel)
        print(f"OK {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
