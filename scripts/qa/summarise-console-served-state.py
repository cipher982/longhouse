#!/usr/bin/env python3
"""Render the Console served-state artifact as a GitHub step summary.

Kept out of the workflow YAML deliberately: an inline heredoc inside a YAML
block scalar parses as valid YAML right up until it doesn't, and a summary
step that crashes hides the result it exists to show.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    providers = data.get("providers") or {str(data.get("provider")): data}
    print("## Console served state")
    print()
    print(f"Verdict: **{data.get('verdict')}**")
    print()
    print("| provider | verdict | failures |")
    print("| --- | --- | --- |")
    for name, report in sorted(providers.items()):
        failures = report.get("failures") or []
        detail = "; ".join(str(item) for item in failures)[:180] or "-"
        print(f"| {name} | {report.get('verdict')} | {detail} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
