#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import zipfile
from pathlib import Path

FORBIDDEN_PREFIXES = (
    "control_plane/",
    "control-plane/",
    "longhouse_shared/",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate built Longhouse wheel archives.")
    parser.add_argument("wheel", type=Path, nargs="+", help="Wheel archive(s) to validate")
    return parser.parse_args()


def validate_wheel(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"wheel not found: {path}")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        counts = collections.Counter(names)

    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        joined = "\n".join(f"  - {name}" for name in duplicates)
        raise SystemExit(f"{path} contains duplicate ZIP entries:\n{joined}")

    forbidden = sorted(name for name in names if name.startswith(FORBIDDEN_PREFIXES))
    if forbidden:
        joined = "\n".join(f"  - {name}" for name in forbidden)
        raise SystemExit(f"{path} contains hosted/control-plane-only files:\n{joined}")


def main() -> int:
    args = parse_args()
    for wheel in args.wheel:
        validate_wheel(wheel)
        print(f"OK {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
