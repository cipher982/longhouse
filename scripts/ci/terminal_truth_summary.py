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

Reruns (``--allow-reruns``, push and dispatched runs; never the nightly). The runner shares its host. In the two 20-pass
runs of 2026-11-12 one cell in 240 had a live median over budget while every marker arrived, in order and complete, and
the host's vmstat showed a neighbour's disk burst (control-plane/docs/specs/terminal-truth-ci-gate.md, "First nightly").
A cell whose ONLY failure is that median may be run once more, alone; the workflow asks which with

  terminal_truth_summary.py --rerun-candidates pass-<i>-<scenario>.json    (prints "provider mode" per line)

and writes each rerun to ``pass-<i>-<scenario>.rerun-<provider>-<mode>.json``. A pass is cleared only if every one of its
failing cells is such a cell, there are at most MAX_RERUNS_PER_PASS of them, and every rerun passes outright. Anything
else (text missing, out of order, a stage before the stream, a slow rerun, many slow cells at once) fails as before.
Both results are kept: the per-cell medians still show the slow first try, and gate.json lists ``passed_on_rerun``, so a
cell that needs reruns often is visible as a count instead of hidden.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

GOOD = {"pass", "known_bad", "not_measurable"}
LATENCY_CHECK = "median_delay_within_budget"
#: More slow cells than this in one pass is a slow product or a slow runner, not one stalled cell.
MAX_RERUNS_PER_PASS = 2


def outcome_of(row: dict) -> str | None:
    return row.get("outcome") or row.get("status")


def model_requests(row: dict) -> int:
    return ((row.get("stages") or {}).get("preflight", {}).get("evidence") or {}).get("model_requests_served", 0)


def slow_only(row: dict) -> bool:
    """The row failed for one reason: its live median was over budget. Every marker arrived, in order, complete."""
    if outcome_of(row) != "fail" or row.get("first_failed_stage") != "stream_delivered":
        return False
    stages = row.get("stages") or {}
    if any(not (stage or {}).get("ok") for name, stage in stages.items() if name != "stream_delivered"):
        return False
    checks = ((stages.get("stream_delivered") or {}).get("evidence") or {}).get("checks") or {}
    return [name for name, check in checks.items() if not (check or {}).get("ok")] == [LATENCY_CHECK]


def rerun_candidates(rows: list[dict]) -> list[dict]:
    """The rows of one pass that may be run once more, or none if the pass cannot be cleared that way."""
    bad = [row for row in rows if outcome_of(row) not in GOOD]
    if not bad or len(bad) > MAX_RERUNS_PER_PASS or not all(slow_only(row) for row in bad):
        return []
    return bad


def cleared_by_rerun(directory: Path, index: int, scenario: str, rows: list[dict]) -> dict[str, dict]:
    """cell -> its passing rerun row, for a pass whose every failing cell passed a rerun; otherwise empty."""
    cleared: dict[str, dict] = {}
    for row in rerun_candidates(rows):
        path = directory / f"pass-{index}-{scenario}.rerun-{row['provider']}-{row['mode']}.json"
        try:
            again = [r for r in json.loads(path.read_text()) if r.get("provider") == row["provider"] and r.get("mode") == row["mode"]]
        except (OSError, json.JSONDecodeError):
            return {}
        if len(again) != 1 or outcome_of(again[0]) != "pass" or model_requests(again[0]) < 1:
            return {}
        cleared[f"{row['provider']}/{row['mode']}"] = again[0]
    return cleared


def live_median(row: dict) -> float | None:
    for lane in row.get("lanes") or []:
        if "SSE" in str(lane.get("lane", "")):
            return lane.get("median_delay_s")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path, nargs="?")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--scenarios", default="long_reply")
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--allow-reruns", action="store_true", help="accept rerun files for slow-median-only cells")
    parser.add_argument("--rerun-candidates", type=Path, metavar="PASS_JSON", help="print the cells of one pass to run once more")
    args = parser.parse_args(argv)
    if args.rerun_candidates is not None:
        try:
            rows = json.loads(args.rerun_candidates.read_text())
        except (OSError, json.JSONDecodeError):
            return 0  # no readable result: nothing to rerun, and the verdict fails on the missing file
        for row in rerun_candidates(rows):
            print(row["provider"], row["mode"])
        return 0
    if args.directory is None:
        parser.error("DIR is required")

    scenarios = [s for s in args.scenarios.split(",") if s]
    problems: list[str] = []
    failing: dict[str, str] = {}  # "cell [scenario]" -> reason
    medians: dict[str, list[float | None]] = {}  # "cell [scenario]" -> one median per pass
    served_none: list[str] = []
    passed_on_rerun: list[dict] = []
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
            cleared = cleared_by_rerun(args.directory, i, scenario, rows) if args.allow_reruns else {}
            for row in rows:
                cell = row.get("cell") or row["provider"] + "/" + row["mode"]
                key = f"{cell} [{scenario}]"
                medians.setdefault(key, []).append(live_median(row))
                outcome = outcome_of(row)
                if outcome not in GOOD and cell in cleared:
                    passed_on_rerun.append(
                        {"cell": key, "pass": i, "first_median_s": live_median(row), "rerun_median_s": live_median(cleared[cell])}
                    )
                elif outcome not in GOOD:
                    reason = f"pass {i}: {outcome} at {row.get('first_failed_stage')}: {(row.get('reason') or '')[:200]}"
                    failing.setdefault(key, reason)
                if row.get("status") == "measured" and model_requests(row) < 1:
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
    reran = f", {len(passed_on_rerun)} on a rerun" if passed_on_rerun else ""
    description = (
        f"terminal truth: {len(medians)} cells x {args.repeats} pass(es) ok{reran}"
        if not problems
        else f"terminal truth: {len(problems)} problem(s): {names or problems[0]}"[:140]
    )
    lines = [f"## Terminal truth: {state}", "", f"{args.repeats} pass(es) of: {', '.join(scenarios)}", ""]
    lines += ["| cell | live median per pass (s): min / median / max | passes |", "|---|---|---|"]
    for key, values in sorted(medians.items()):
        have = [v for v in values if v is not None]
        stats = f"{min(have):.2f} / {statistics.median(have):.2f} / {max(have):.2f}" if have else "none"
        lines.append(f"| {key} | {stats} | {len(values)} |")
    if passed_on_rerun:
        lines += ["", "Passed on a rerun (the first try's only failure was a slow live median):"]
        lines += [
            f"- {item['cell']} pass {item['pass']}: first {item['first_median_s']} s, rerun {item['rerun_median_s']} s"
            for item in passed_on_rerun
        ]
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
        "passed_on_rerun": passed_on_rerun,
        "problems": problems,
        "medians": medians,
    }
    (args.directory / "gate.json").write_text(json.dumps(gate, indent=2) + "\n")
    print(text)
    for item in passed_on_rerun:
        print(
            f"::warning::terminal truth passed on a rerun {item['cell']} pass {item['pass']}: "
            f"first median {item['first_median_s']} s, rerun {item['rerun_median_s']} s"
        )
    for key in new:
        label = "NEW failure" if args.previous else "failure"
        print(f"::error::terminal truth {label} {key}: {failing[key]}")
    for key in sorted(set(failing) - set(new)):
        print(f"::warning::terminal truth still failing {key}")
    for problem in problems:
        if not any(problem.startswith(k) for k in failing):
            print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
