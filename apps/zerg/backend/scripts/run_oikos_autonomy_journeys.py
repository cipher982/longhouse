#!/usr/bin/env python3
"""Run Oikos autonomy journey fixtures and persist reviewable artifacts.

Usage:
  uv run python apps/zerg/backend/scripts/run_oikos_autonomy_journeys.py
  uv run python apps/zerg/backend/scripts/run_oikos_autonomy_journeys.py --artifact-root /tmp/oikos-autonomy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.services.oikos_autonomy_journeys import DEFAULT_AUTONOMY_ARTIFACT_ROOT
from zerg.services.oikos_autonomy_journeys import DEFAULT_AUTONOMY_JOURNEY_FIXTURE_PATH
from zerg.services.oikos_autonomy_journeys import load_autonomy_journey_cases
from zerg.services.oikos_autonomy_journeys import run_autonomy_journeys


def _build_run_root(base_root: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = base_root / timestamp
    run_root.mkdir(parents=True, exist_ok=False)
    return run_root


async def _run(args: argparse.Namespace) -> int:
    fixture_path = Path(args.fixture).expanduser().resolve()
    artifact_root = Path(args.artifact_root).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    run_root = _build_run_root(artifact_root)

    cases = load_autonomy_journey_cases(fixture_path)
    results = await run_autonomy_journeys(
        fixture_path=fixture_path,
        artifact_root=run_root,
    )

    summary = {
        "fixture_path": str(fixture_path),
        "artifact_root": str(run_root),
        "cases": [
            {
                "case_id": case.id,
                "expected_decision": case.expected.decision,
                "actual_decision": result.decision.decision,
                "needs_human": result.decision.needs_human,
                "proposed_action_count": len(result.decision.proposed_actions),
                "run_dir": str(result.run_dir),
            }
            for case, result in zip(cases, results, strict=True)
        ],
    }
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    print(f"Autonomy journeys: {len(results)} case(s)")
    print(f"Fixture: {fixture_path}")
    print(f"Artifacts: {run_root}")
    for case, result in zip(cases, results, strict=True):
        status = "match" if result.decision.decision == case.expected.decision else "drift"
        print(
            f"- {case.id}: {result.decision.decision} "
            f"(expected {case.expected.decision}, {status}, actions={len(result.decision.proposed_actions)})"
        )
    print(f"Summary: {summary_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Oikos autonomy journey fixtures")
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_AUTONOMY_JOURNEY_FIXTURE_PATH),
        help="Path to the autonomy journey fixture YAML",
    )
    parser.add_argument(
        "--artifact-root",
        default=str(DEFAULT_AUTONOMY_ARTIFACT_ROOT),
        help="Directory where run artifacts should be written",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
