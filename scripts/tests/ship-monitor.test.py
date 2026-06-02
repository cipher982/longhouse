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


def run_info(
    workflow_name: str,
    run_id: int,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "push",
) -> object:
    return ship_monitor.RunInfo(
        databaseId=run_id,
        workflowName=workflow_name,
        status=status,
        conclusion=conclusion,
        url=f"https://example.test/runs/{run_id}",
        event=event,
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


def test_gate_heartbeat_names_blocking_ci_job_and_step() -> None:
    def fake_fetch_run_jobs(repo: str, run_id: int) -> list[dict[str, object]]:
        if run_id == 1:
            return [
                {
                    "name": ship_monitor.DEPLOY_GATE_JOB,
                    "status": "in_progress",
                    "steps": [
                        {"name": "Wait for full CI gate", "status": "in_progress"},
                    ],
                }
            ]
        if run_id == 2:
            return [
                {
                    "name": "iOS tests",
                    "status": "in_progress",
                    "steps": [
                        {"name": "Run iOS tests", "status": "in_progress"},
                    ],
                }
            ]
        return []

    ship_monitor.fetch_run_jobs = fake_fetch_run_jobs
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1, status="in_progress", conclusion=None),
        run_info(ship_monitor.CI_WORKFLOW, 2, status="in_progress", conclusion=None),
    ]

    summary = ship_monitor.summarize_incomplete_runs("cipher982/longhouse", "abc123", runs)

    assert "Deploy and Verify #1 / gate -> CI #2 / iOS tests / Run iOS tests: in_progress" in summary


def test_deploy_heartbeat_names_active_deploy_step() -> None:
    def fake_fetch_run_jobs(repo: str, run_id: int) -> list[dict[str, object]]:
        return [
            {
                "name": ship_monitor.DEPLOY_GATE_JOB,
                "status": "completed",
                "steps": [],
            },
            {
                "name": ship_monitor.DEPLOY_AND_VERIFY_JOB,
                "status": "in_progress",
                "steps": [
                    {"name": "Deploy public demo runtime", "status": "in_progress"},
                ],
            },
        ]

    ship_monitor.fetch_run_jobs = fake_fetch_run_jobs
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1, status="in_progress", conclusion=None),
    ]

    summary = ship_monitor.summarize_incomplete_runs("cipher982/longhouse", "abc123", runs)

    assert (
        "Deploy and Verify #1 / Deploy demo + canary + fast smoke / "
        "Deploy public demo runtime: in_progress"
    ) in summary


def test_manual_deploy_recovery_supersedes_failed_push_deploy() -> None:
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1, conclusion="failure"),
        run_info(ship_monitor.CI_WORKFLOW, 2),
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 3, event="workflow_dispatch"),
    ]

    selected, required_names = ship_monitor.select_load_bearing_runs(runs)

    assert required_names == [ship_monitor.DEPLOY_AND_VERIFY]
    assert [run.databaseId for run in selected] == [3]
    assert ship_monitor.runs_succeeded(selected)


if __name__ == "__main__":
    test_runtime_reuse_does_not_require_exact_live_sha()
    test_runtime_publish_requires_exact_live_sha()
    test_gate_heartbeat_names_blocking_ci_job_and_step()
    test_deploy_heartbeat_names_active_deploy_step()
    test_manual_deploy_recovery_supersedes_failed_push_deploy()
    print("ship-monitor tests passed")
