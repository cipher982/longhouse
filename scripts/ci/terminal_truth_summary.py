#!/usr/bin/env python3
"""Read the result files of one terminal-truth run and decide the lane's verdict.

  terminal_truth_summary.py DIR [--repeats N] [--gate-profile] [--previous DIR]

DIR is the ``--out-dir`` of ``make terminal-truth-all`` (server/zerg/qa/terminal_truth_parallel.py): ``fleet.json`` (what
the run planned, each unmutated cell's verdict, what was run again), ``pass-<i>-<scenario>.json`` (the reply cells' full
rows), ``kinds.json`` and ``mutations.json``. The run's own exit status is not trusted. The verdict is a failure when

- fleet.json is missing or unreadable (the run did not finish), or it planned no cell;
- a planned cell has no verdict, or one other than pass or finding;
- a planned mutation check has no row in mutations.json, or a row that is not ok;
- a reply pass file is missing, has a row whose outcome is not pass, known_bad or not_measurable, or has fewer rows than
  the first pass (coverage floor);
- a measured reply row's scripted model served no request (the harness already treats this as a preflight failure; this is
  the independent fail-closed check);
- ``--gate-profile`` is given and the run was narrowed (fewer scenarios, providers, modes or mutation checks, or another
  stream than the gate's): the commit status means the whole gate ran.

``--repeats N`` also requires ``pass-<i>-<scenario>.json`` for i up to N (a hand-started run for the budget record).

Reported and not failed: a cell that came out wrong beside the others and right when run again alone (``reruns`` in
fleet.json, ``passed_on_rerun`` in gate.json, a warning each), and a state shown late on a cell where that is declared
known (``finding``). The nightly runs with reruns off, so its red runs are the raw rate.

Writes DIR/gate.json and DIR/summary.md, prints the summary, and exits 1 on failure. ``--previous`` is the last nightly's
gate.json: a failing cell that was not failing then is annotated as a NEW failure. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

GOOD = {"pass", "known_bad", "not_measurable"}
GOOD_CELL = {"pass", "finding"}
REPLY_SCENARIOS = ("long_reply", "two_message")


def outcome_of(row: dict) -> str | None:
    return row.get("outcome") or row.get("status")


def model_requests(row: dict) -> int:
    return ((row.get("stages") or {}).get("preflight", {}).get("evidence") or {}).get("model_requests_served", 0)


def live_median(row: dict) -> float | None:
    for lane in row.get("lanes") or []:
        if "SSE" in str(lane.get("lane", "")):
            return lane.get("median_delay_s")
    return None


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def one_line(text: Any, limit: int = 240) -> str:
    return " ".join(str(text or "").split())[:limit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--gate-profile", action="store_true", help="fail a run that was narrowed")
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)
    directory: Path = args.directory

    problems: list[str] = []
    failing: dict[str, str] = {}  # "cell [scenario]" -> reason
    medians: dict[str, list[float | None]] = {}  # "cell [scenario]" -> one median per pass
    findings: list[str] = []
    passed_on_rerun: list[dict] = []
    mutation_misses: list[str] = []

    fleet = read_json(directory / "fleet.json")
    if not isinstance(fleet, dict):
        problems.append("no readable fleet.json: the run did not finish")
        fleet = {}
    planned = fleet.get("planned") or {}
    planned_cells: list[str] = list(planned.get("cells") or [])
    planned_mutations: list[str] = list(planned.get("mutations") or [])
    if fleet and not planned_cells:
        problems.append("the run planned no cell")
    if args.gate_profile and fleet and fleet.get("narrowed") is not False:
        problems.append("the run was narrowed: the gate is every scenario, provider and mode and every mutation check")

    # Every planned cell needs a good verdict.
    verdicts = {str(cell.get("id")): cell for cell in fleet.get("cells") or []}
    for cell_id in planned_cells:
        scenario, _, cell = cell_id.partition(":")
        key = f"{cell} [{scenario}]"
        verdict = verdicts.get(cell_id)
        if verdict is None:
            failing[key] = "no verdict for this planned cell"
        elif verdict.get("outcome") == "finding":
            findings.append(f"{key}: {one_line(verdict.get('reason'))}")
        elif verdict.get("outcome") not in GOOD_CELL:
            failing[key] = (
                f"{verdict.get('verdict') or verdict.get('outcome')} at {verdict.get('stage')}: {one_line(verdict.get('reason'), 200)}"
            )

    # Every planned mutation check needs an ok row.
    table = read_json(directory / "mutations.json") if planned_mutations else []
    rows_by_name = {str(row.get("name")): row for row in table or [] if isinstance(row, dict)}
    for name in planned_mutations:
        row = rows_by_name.get(name)
        if row is None:
            mutation_misses.append(f"mutation check {name}: no row in mutations.json")
        elif row.get("ok") is not True:
            if row.get("baseline", "pass") != "pass":
                why = f"its unmutated cell came out {row.get('baseline')} at {row.get('baseline_stage')}: {one_line(row.get('baseline_reason'))}"
            else:
                why = (
                    f"expected {row.get('expected_stage')}, saw {row.get('observed_outcome')} at {row.get('observed_stage')}: "
                    f"{one_line(row.get('reason'))}"
                )
            mutation_misses.append(f"mutation check {name} ({row.get('cell')}): {why}")
    problems += mutation_misses

    # The reply cells' own rows: the fail-closed checks that do not read the run's verdicts.
    scenarios = [s for s in REPLY_SCENARIOS if any(cell_id.startswith(f"{s}:") for cell_id in planned_cells)]
    served_none: list[str] = []
    expected_rows: dict[str, int] = {}
    for scenario in scenarios:
        for i in range(1, args.repeats + 1):
            path = directory / f"pass-{i}-{scenario}.json"
            rows = read_json(path)
            if not isinstance(rows, list):
                problems.append(f"pass {i} ({scenario}): no readable result file")
                continue
            expected_rows.setdefault(scenario, len(rows))
            if len(rows) < expected_rows[scenario]:
                problems.append(f"pass {i} ({scenario}): {len(rows)} rows, the first pass had {expected_rows[scenario]}")
            for row in rows:
                cell = row.get("cell") or row["provider"] + "/" + row["mode"]
                key = f"{cell} [{scenario}]"
                medians.setdefault(key, []).append(live_median(row))
                outcome = outcome_of(row)
                if outcome not in GOOD:
                    failing.setdefault(key, f"pass {i}: {outcome} at {row.get('first_failed_stage')}: {one_line(row.get('reason'), 200)}")
                if row.get("status") == "measured" and model_requests(row) < 1:
                    served_none.append(f"{key} pass {i}")
    for item in served_none:
        problems.append(f"{item}: the fake model served no request")
    for key, reason in failing.items():
        problems.append(f"{key}: {reason}")

    for item in fleet.get("reruns") or []:
        scenario, _, cell = str(item.get("id", "")).partition(":")
        passed_on_rerun.append(
            {"id": item.get("id"), "cell": f"{cell} [{scenario}]", "first": item.get("first"), "again": item.get("again")}
        )

    notes: list[str] = []
    lane = fleet.get("embedding_lane")
    if isinstance(lane, dict) and lane.get("loaded") is not True:
        notes.append(f"the embedding model did not load in {lane.get('shard')}, so no cell ran beside the embedding lane")
    if fleet.get("jobs_asked") not in (None, fleet.get("jobs")):
        notes.append(f"{fleet.get('jobs')} shards ran at a time, not the {fleet.get('jobs_asked')} asked for: memory held no more")

    previous_failing: set[str] = set()
    if args.previous and (args.previous / "gate.json").is_file():
        previous_failing = set((read_json(args.previous / "gate.json") or {}).get("failing_cells", []))
    new = sorted(set(failing) - previous_failing) if args.previous else sorted(failing)

    state = "failure" if problems else "success"
    names = ", ".join(sorted(failing)[:4])
    reran = f", {len(passed_on_rerun)} run again alone" if passed_on_rerun else ""
    wall = fleet.get("wall_s")
    took = f" in {wall:.0f} s" if isinstance(wall, (int, float)) else ""
    description = (
        f"terminal truth: {len(planned_cells)} cells, {len(planned_mutations)} mutation checks ok{took}{reran}"
        if not problems
        else f"terminal truth: {len(problems)} problem(s): {names or problems[0]}"[:140]
    )
    by_scenario: dict[str, list[str]] = {}
    for cell_id in planned_cells:
        by_scenario.setdefault(cell_id.partition(":")[0], []).append(cell_id)
    lines = [f"## Terminal truth: {state}", ""]
    memory = fleet.get("memory") or {}
    peak = f" Memory in use went from {memory.get('before_gib')} to {memory.get('peak_gib')} GiB." if memory.get("peak_gib") else ""
    lines += [
        f"{len(planned_cells)} cells and {len(planned_mutations)} mutation checks{took}, {fleet.get('jobs', '?')} shards at a time.{peak}"
    ]
    builds = fleet.get("mutated_engine_builds") or {}
    if builds:
        lines += [f"Mutated engines: {builds.get('hits')} from the cache, {builds.get('built')} built ({builds.get('seconds')} s)."]
    lines += ["", "| scenario | cells | not good |", "|---|---|---|"]
    for scenario, ids in by_scenario.items():
        bad = [i.partition(":")[2] for i in ids if (verdicts.get(i) or {}).get("outcome") not in GOOD_CELL]
        lines.append(f"| {scenario} | {len(ids)} | {', '.join(bad) or '-'} |")
    if planned_mutations:
        lines.append(f"| mutation checks | {len(planned_mutations)} | {len(mutation_misses) or '-'} |")
    if medians:
        lines += ["", "| reply cell | live median per pass (s): min / median / max | passes |", "|---|---|---|"]
        for key, values in sorted(medians.items()):
            have = [v for v in values if v is not None]
            stats = f"{min(have):.2f} / {statistics.median(have):.2f} / {max(have):.2f}" if have else "none"
            lines.append(f"| {key} | {stats} | {len(values)} |")
    if passed_on_rerun:
        lines += ["", "Came out wrong beside the others and right when run again alone:"]
        lines += [f"- {item['id']}: first {one_line(item['first'])}; alone {one_line(item['again'])}" for item in passed_on_rerun]
    if findings:
        lines += ["", "Findings (a state shown late on a cell where that is declared known; reported, not failed):"]
        lines += [f"- {item}" for item in findings]
    if notes:
        lines += ["", "Notes:"] + [f"- {note}" for note in notes]
    if problems:
        lines += ["", "Problems:"] + [f"- {p}" for p in problems]
    text = "\n".join(lines) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.md").write_text(text)
    gate = {
        "state": state,
        "description": description,
        "repeats": args.repeats,
        "scenarios": sorted(by_scenario),
        "cells": len(planned_cells),
        "mutation_checks": len(planned_mutations),
        "wall_s": wall,
        "failing_cells": sorted(failing),
        "mutation_misses": mutation_misses,
        "passed_on_rerun": passed_on_rerun,
        "findings": findings,
        "notes": notes,
        "problems": problems,
        "medians": medians,
    }
    (directory / "gate.json").write_text(json.dumps(gate, indent=2) + "\n")
    print(text)
    for item in passed_on_rerun:
        print(f"::warning::terminal truth {item['id']} passed only when run again alone: first {one_line(item['first'], 160)}")
    for item in findings:
        print(f"::warning::terminal truth finding {one_line(item, 200)}")
    for note in notes:
        print(f"::warning::terminal truth: {note}")
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
