#!/usr/bin/env python3
"""Focused tests for the Console served-state GitHub summary."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa" / "summarise-console-served-state.py"


def _render(payload: dict) -> str:
    with tempfile.TemporaryDirectory() as temp_dir:
        artifact = Path(temp_dir) / "console-served-state.json"
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(artifact)],
            check=False,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_matrix_summary_counts_verdicts_and_escapes_failure_cells() -> None:
    output = _render(
        {
            "verdict": "red",
            "providers": {
                "ci/codex": {"verdict": "error", "failures": ["not authenticated | retry\nneeded"]},
                "ci/omp": {"verdict": "green", "failures": []},
                "ci/antigravity": {"verdict": "unavailable", "failures": ["no durable reply"]},
            },
        }
    )

    assert "Providers: 3; green: 1; red/error: 1; unavailable: 1" in output
    assert "| ci/codex | error | not authenticated \\| retry<br>needed |" in output
    assert "| ci/omp | green | - |" in output
    assert "| ci/antigravity | unavailable | no durable reply |" in output


def test_single_provider_summary_uses_provider_name_when_no_matrix() -> None:
    output = _render(
        {
            "provider": "antigravity",
            "verdict": "red",
            "failures": ["provider output was unavailable"],
        }
    )

    assert "Providers: 1; green: 0; red/error: 1; unavailable: 0" in output
    assert "| antigravity | red | provider output was unavailable |" in output


def main() -> int:
    tests = [
        test_matrix_summary_counts_verdicts_and_escapes_failure_cells,
        test_single_provider_summary_uses_provider_name_when_no_matrix,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
