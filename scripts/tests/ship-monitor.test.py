#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "ops" / "ship-monitor.py"

spec = importlib.util.spec_from_file_location("ship_monitor", MODULE_PATH)
assert spec is not None
ship_monitor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ship_monitor
spec.loader.exec_module(ship_monitor)


DEPLOY_STATUS_LATEST = """

Surface              SHA          Health     Uptime
-------              ---          ------     ------
Demo runtime         latest       healthy    Up 2 minutes (healthy)
Control plane        f3e42620e7   ok         Up 2 days (healthy)
Canary               latest       healthy    Up 39 seconds (healthy)
Local HEAD           ac77b06d72

"""


def run_info(workflow_name: str, run_id: int) -> object:
    return ship_monitor.RunInfo(
        databaseId=run_id,
        workflowName=workflow_name,
        status="completed",
        conclusion="success",
        url=f"https://example.test/runs/{run_id}",
    )


def with_fakes(job_conclusions: dict[int, dict[str, str]]) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=DEPLOY_STATUS_LATEST, stderr="")

    def fake_fetch_run_jobs(repo: str, run_id: int) -> list[dict[str, str]]:
        return [
            {"name": name, "conclusion": conclusion}
            for name, conclusion in job_conclusions.get(run_id, {}).items()
        ]

    ship_monitor.run = fake_run
    ship_monitor.fetch_run_jobs = fake_fetch_run_jobs


def test_runtime_reuse_does_not_require_exact_live_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "skipped"},
        }
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, raw = ship_monitor.verify_live_state(ROOT, "cipher982/longhouse", "ac77b06d72", runs)

    assert errors == []
    assert "Local HEAD" in raw
    assert "differs from deployed demo" not in raw


def test_runtime_publish_requires_exact_live_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "success"},
        }
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(ROOT, "cipher982/longhouse", "ac77b06d72", runs)

    assert "Demo runtime is on latest, expected ac77b06d72" in errors
    assert "Canary is on latest, expected ac77b06d72" in errors


if __name__ == "__main__":
    test_runtime_reuse_does_not_require_exact_live_sha()
    test_runtime_publish_requires_exact_live_sha()
    print("ship-monitor tests passed")
