#!/usr/bin/env python3
"""Render the Console served-state artifact as a GitHub step summary.

Kept out of the workflow YAML deliberately: an inline heredoc inside a YAML
block scalar parses as valid YAML right up until it doesn't, and a summary
step that crashes hides the result it exists to show.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def _markdown_cell(value: object) -> str:
    """Keep provider errors from corrupting the summary table."""
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _failure_detail(report: object) -> str:
    if not isinstance(report, dict):
        return "missing report"
    failures = report.get("failures") or []
    detail = "; ".join(str(item) for item in failures)[:180] or "-"
    return _markdown_cell(detail)


def _provider_reports(data: dict) -> dict[str, object]:
    providers = data.get("providers")
    if isinstance(providers, dict) and providers:
        return {str(name): report for name, report in providers.items()}
    return {str(data.get("provider") or "unknown"): data}


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    providers = _provider_reports(data)
    verdicts = collections.Counter(
        report.get("verdict")
        for report in providers.values()
        if isinstance(report, dict)
    )
    print("## Console served state")
    print()
    print(f"Verdict: **{_markdown_cell(data.get('verdict'))}**")
    print(
        "Providers: "
        f"{len(providers)}; "
        f"green: {verdicts.get('green', 0)}; "
        f"red/error: {verdicts.get('red', 0) + verdicts.get('error', 0)}; "
        f"unavailable: {verdicts.get('unavailable', 0)}"
    )
    print()
    print("| provider | verdict | failures |")
    print("| --- | --- | --- |")
    for name, report in sorted(providers.items()):
        verdict = report.get("verdict") if isinstance(report, dict) else "missing"
        print(f"| {_markdown_cell(name)} | {_markdown_cell(verdict)} | {_failure_detail(report)} |")




if __name__ == "__main__":
    raise SystemExit(main())
