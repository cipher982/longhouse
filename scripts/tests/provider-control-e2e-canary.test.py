#!/usr/bin/env python3
"""Tests for hermetic managed-provider control E2E canaries."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts/qa/provider-control-e2e-canary.py"


def _run_canary(root: Path, args: list[str]) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "artifact.json"
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(REPO_ROOT),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
            *args,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return result, payload


def test_all_current_provider_control_paths_are_green() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        result, payload = _run_canary(Path(temp_dir), ["--provider", "all"])

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert set(payload["canaries"]) == {"claude", "opencode", "antigravity"}

        claude = payload["canaries"]["claude"]
        assert claude["status"] == "pass"
        assert claude["steer_meta"]["intent"] == "steer"

        opencode = payload["canaries"]["opencode"]
        assert opencode["status"] == "pass"
        assert {"serve", "session.create", "prompt_async", "abort", "attach"} <= set(opencode["observed_events"])

        antigravity = payload["canaries"]["antigravity"]
        assert antigravity["status"] == "pass"
        assert antigravity["post_injection"]["terminationBehavior"] == "force_continue"
        assert antigravity["stop_decision"]["decision"] == "continue"


def test_provider_selection_runs_one_control_lane() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        result, payload = _run_canary(Path(temp_dir), ["--provider", "opencode"])

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert set(payload["canaries"]) == {"opencode"}
        assert payload["canaries"]["opencode"]["status"] == "pass"


def main() -> int:
    tests = [
        test_all_current_provider_control_paths_are_green,
        test_provider_selection_runs_one_control_lane,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
