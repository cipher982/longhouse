#!/usr/bin/env python3
"""Read the result files of one or more terminal-truth passes and decide the lane's verdict.

  terminal_truth_summary.py DIR --repeats N --scenarios long_reply[,two_message] [--previous DIR]

DIR holds ``pass-<i>-<scenario>.json`` (the harness's ``--out`` file: one row per cell). The verdict is a failure when

- a pass file is missing (a pass that crashed before writing, or was never run);
- any row has an outcome other than pass, known_bad or not_measurable;
- a pass has fewer rows than the first pass (coverage floor);
- a measured row's scripted model served no request (the harness already treats this as a preflight failure; this is the
  independent fail-closed check).

Writes DIR/gate.json (state, description, failing cells, per-cell live medians) and DIR/summary.md, prints the summary, and
exits 1 on failure. ``--previous`` is the last nightly's gate.json: a failing cell that was not failing then is annotated as a
NEW failure. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

GOOD = {"pass", "known_bad", "not_measurable"}


def live_median(row: dict) -> float | None:
    for lane in row.get("lanes") or []:
        if "SSE" in str(lane.get("lane", "")):
            return lane.get("median_delay_s")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--scenarios", default="long_reply")
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)

    scenarios = [s for s in args.scenarios.split(",") if s]
    problems: list[str] = []
    failing: dict[str, str] = {}  # "cell [scenario]" -> reason
    medians: dict[str, list[float | None]] = {}  # "cell [scenario]" -> one median per pass
    served_none: list[str] = []
    expected_rows: dict[str, int] = {}
    rows_total = 0
    for scenario in scenarios:
        for i in range(1, args.repeats + 1):
            path = args.directory / f"pass-{i}-{scenario}.json"
            if not path.is_file():
                problems.append(f"pass {i} ({scenario}): no result file")
                continue
            try:
                rows = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                problems.append(f"pass {i} ({scenario}): unreadable result file: {exc}")
                continue
            rows_total += len(rows)
            expected_rows.setdefault(scenario, len(rows))
            if len(rows) < expected_rows[scenario]:
                problems.append(f"pass {i} ({scenario}): {len(rows)} rows, the first pass had {expected_rows[scenario]}")
            for row in rows:
                key = f"{row.get('cell') or row['provider'] + '/' + row['mode']} [{scenario}]"
                medians.setdefault(key, []).append(live_median(row))
                outcome = row.get("outcome") or row.get("status")
                if outcome not in GOOD:
                    reason = f"pass {i}: {outcome} at {row.get('first_failed_stage')}: {(row.get('reason') or '')[:200]}"
                    failing.setdefault(key, reason)
                requests = ((row.get("stages") or {}).get("preflight", {}).get("evidence") or {}).get("model_requests_served", 0)
                if row.get("status") == "measured" and requests < 1:
                    served_none.append(f"{key} pass {i}")
    for item in served_none:
        problems.append(f"{item}: the fake model served no request")
    for key, reason in failing.items():
        problems.append(f"{key}: {reason}")

    previous_failing: set[str] = set()
    if args.previous and (args.previous / "gate.json").is_file():
        previous_failing = set(json.loads((args.previous / "gate.json").read_text()).get("failing_cells", []))
    new = sorted(set(failing) - previous_failing) if args.previous else sorted(failing)

    state = "failure" if problems else "success"
    names = ", ".join(sorted(failing)[:4])
    description = (
        f"terminal truth: {len(medians)} cells x {args.repeats} pass(es) ok"
        if not problems
        else f"terminal truth: {len(problems)} problem(s): {names or problems[0]}"[:140]
    )
    lines = [f"## Terminal truth: {state}", "", f"{args.repeats} pass(es) of: {', '.join(scenarios)}", ""]
    lines += ["| cell | live median per pass (s): min / median / max | passes |", "|---|---|---|"]
    for key, values in sorted(medians.items()):
        have = [v for v in values if v is not None]
        stats = f"{min(have):.2f} / {statistics.median(have):.2f} / {max(have):.2f}" if have else "none"
        lines.append(f"| {key} | {stats} | {len(values)} |")
    if problems:
        lines += ["", "Problems:"] + [f"- {p}" for p in problems]
    text = "\n".join(lines) + "\n"
    (args.directory / "summary.md").write_text(text)
    gate = {
        "state": state,
        "description": description,
        "repeats": args.repeats,
        "scenarios": scenarios,
        "failing_cells": sorted(failing),
        "problems": problems,
        "medians": medians,
    }
    (args.directory / "gate.json").write_text(json.dumps(gate, indent=2) + "\n")
    print(text)
    for key in new:
        print(f"::error::terminal truth NEW failure {key}: {failing[key]}")
    for key in sorted(set(failing) - set(new)):
        print(f"::warning::terminal truth still failing {key}")
    for problem in problems:
        if not any(problem.startswith(k) for k in failing):
            print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
