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
    # The provider factory's test machinery — the universal agent harness,
    # per-provider adapters, release canaries, qualification profiles. Repo
    # and CI only; no Longhouse user ever runs it, and the hosted Runtime Host
    # builds from server/ source rather than this wheel. Excluded in
    # pyproject; asserted here so a new zerg/qa module that a shipped module
    # happens to import cannot quietly put ~940 KB of it back on PyPI.
    "zerg/qa/",
)

# Files a shipped module must be able to import at runtime. The capability
# endpoint reads the declared provider contract on every request, and a
# pip-installed wheel has neither a repo checkout above it nor the runtime
# image's /schemas — so if this is missing, /api/agents/provider-capabilities
# 500s for every self-hoster.
REQUIRED_MEMBERS = ("zerg/_config/managed_providers.yml",)


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

    present = set(names)
    missing = sorted(member for member in REQUIRED_MEMBERS if member not in present)
    if missing:
        joined = "\n".join(f"  - {name}" for name in missing)
        raise SystemExit(f"{path} is missing files a shipped module imports at runtime:\n{joined}")


def main() -> int:
    args = parse_args()
    for wheel in args.wheel:
        validate_wheel(wheel)
        print(f"OK {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
