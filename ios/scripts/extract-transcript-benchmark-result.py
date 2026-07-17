#!/usr/bin/env python3
"""Extract the structured iOS transcript benchmark result from xcodebuild output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


MARKER = "TRANSCRIPT_BENCHMARK_RESULT "


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Add run metadata that is known by the xcodebuild wrapper.",
    )
    args = parser.parse_args()

    matches: list[dict[str, object]] = []
    for line in args.log.read_text(encoding="utf-8", errors="replace").splitlines():
        marker_index = line.find(MARKER)
        if marker_index < 0:
            continue
        raw = line[marker_index + len(MARKER) :].strip()
        matches.append(json.loads(raw))

    if not matches:
        raise SystemExit(f"No {MARKER.strip()} line found in {args.log}")

    result = matches[-1]
    for item in args.set:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise SystemExit(f"Invalid --set value: {item!r}; expected KEY=VALUE")
        result[key] = value

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
