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


def deploy_status(demo_sha: str, canary_sha: str, *, demo_health: str = "healthy", canary_health: str = "healthy") -> str:
    return f"""

Surface              SHA          Health     Uptime
-------              ---          ------     ------
Demo runtime         {demo_sha}   {demo_health}    Up 2 minutes ({demo_health})
Control plane        f3e42620e7   ok         Up 2 days (healthy)
Canary               {canary_sha}   {canary_health}    Up 39 seconds ({canary_health})
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


def with_fakes(
    job_conclusions: dict[int, dict[str, str]],
    *,
    latest_runtime_sha: str | None = "latest",
    deploy_status_output: str | list[str] | None = None,
    ancestry_path_shas: list[str] | None = None,
) -> None:
    deploy_status_outputs = (
        deploy_status_output
        if isinstance(deploy_status_output, list)
        else [deploy_status_output]
    )
    deploy_status_index = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal deploy_status_index
        cmd = args[0] if args else []
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            if cmd[:3] == ["git", "rev-list", "--ancestry-path"]:
                stdout = "\n".join(ancestry_path_shas or [])
                if stdout:
                    stdout += "\n"
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=stdout,
                    stderr="",
                )
            return subprocess.run(
                cmd,
                cwd=kwargs.get("cwd"),
                text=True,
                capture_output=True,
                check=False,
            )
        output = deploy_status_outputs[min(deploy_status_index, len(deploy_status_outputs) - 1)]
        deploy_status_index += 1
        if output is None:
            output = deploy_status("latest", "latest")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    def fake_fetch_run_jobs(repo: str, run_id: int) -> list[dict[str, str]]:
        return [
            {"name": name, "conclusion": conclusion}
            for name, conclusion in job_conclusions.get(run_id, {}).items()
        ]

    ship_monitor.run = fake_run
    ship_monitor.fetch_run_jobs = fake_fetch_run_jobs
    ship_monitor.latest_runtime_affecting_sha = lambda root, target_sha: latest_runtime_sha


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


def test_runtime_reuse_accepts_deploy_stamped_target_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "skipped"},
        },
        latest_runtime_sha="7e917a42689f626ed83908f7ab0a6ab21c3aafc4",
        deploy_status_output=deploy_status("ac77b06d72", "ac77b06d72"),
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(ROOT, "cipher982/longhouse", "ac77b06d72", runs)

    assert errors == []


def test_runtime_reuse_accepts_intermediate_deploy_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "skipped"},
        },
        latest_runtime_sha="7447df0799c06120fa254f0732a7d13646562390",
        deploy_status_output=deploy_status("f45edcb318", "f45edcb318"),
        ancestry_path_shas=["f45edcb3180000000000000000000000000000000"],
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(
        ROOT,
        "cipher982/longhouse",
        "9f90ad4549c002486e07d7a6911e9401de6c65b9",
        runs,
    )

    assert errors == []


def test_runtime_reuse_accepts_intermediate_sha_when_deploy_job_is_absent() -> None:
    with_fakes(
        {
            1: {},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "skipped"},
        },
        latest_runtime_sha="7447df0799c06120fa254f0732a7d13646562390",
        deploy_status_output=deploy_status("f45edcb318", "f45edcb318"),
        ancestry_path_shas=["f45edcb3180000000000000000000000000000000"],
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(
        ROOT,
        "cipher982/longhouse",
        "d2f450d2c1973bbdaaec569b5c7b6d00b8ed1efd",
        runs,
    )

    assert errors == []


def test_runtime_publish_requires_exact_live_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "success"},
        },
        latest_runtime_sha="ac77b06d72",
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(ROOT, "cipher982/longhouse", "ac77b06d72", runs)

    assert "Demo runtime is on latest, expected ac77b06d72" in errors
    assert "Canary is on latest, expected ac77b06d72" in errors


def test_runtime_publish_accepts_deploy_stamped_target_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "success"},
        },
        latest_runtime_sha="7e917a42689f626ed83908f7ab0a6ab21c3aafc4",
        deploy_status_output=deploy_status("ac77b06d72", "ac77b06d72"),
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(ROOT, "cipher982/longhouse", "ac77b06d72", runs)

    assert errors == []


def test_live_verify_accepts_degraded_runtime_health() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
            2: {ship_monitor.RUNTIME_IMAGE_JOB: "success"},
        },
        latest_runtime_sha="7e917a42689f626ed83908f7ab0a6ab21c3aafc4",
        deploy_status_output=deploy_status(
            "ac77b06d72",
            "ac77b06d72",
            demo_health="degraded",
            canary_health="degraded",
        ),
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
        run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(ROOT, "cipher982/longhouse", "ac77b06d72", runs)

    assert errors == []


def test_live_verify_retries_transient_canary_status_gap() -> None:
    original_sleep = ship_monitor.time.sleep
    sleeps: list[float] = []
    ship_monitor.time.sleep = lambda seconds: sleeps.append(seconds)
    try:
        with_fakes(
            {
                1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "success"},
                2: {ship_monitor.RUNTIME_IMAGE_JOB: "success"},
            },
            latest_runtime_sha="5c7933e0a4ee57329f03e23247bce26e311e3cdb",
            deploy_status_output=[
                deploy_status("41818df9fd", "-"),
                deploy_status("41818df9fd", "41818df9fd"),
            ],
            ancestry_path_shas=[
                "5329d01c9b5265189df9164a06b128bb47df8482",
                "41818df9fd5e381bfb12f45f9c4a5a5618c28a3d",
            ],
        )
        runs = [
            run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
            run_info(ship_monitor.RUNTIME_IMAGE_WORKFLOW, 2),
        ]

        _surfaces, errors, _raw = ship_monitor.verify_live_state(
            ROOT,
            "cipher982/longhouse",
            "5329d01c9b5265189df9164a06b128bb47df8482",
            runs,
        )

        assert errors == []
        assert sleeps == [2]
    finally:
        ship_monitor.time.sleep = original_sleep


def test_skipped_tip_still_requires_latest_runtime_affecting_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.DEPLOY_AND_VERIFY_JOB: "skipped"},
        },
        latest_runtime_sha="7e917a42689f626ed83908f7ab0a6ab21c3aafc4",
        deploy_status_output=deploy_status("edb88b9ebe", "edb88b9ebe"),
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1, conclusion="skipped"),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(ROOT, "cipher982/longhouse", "7ede50e79d", runs)

    assert "Demo runtime is on edb88b9ebe, expected 7e917a4268" in errors
    assert "Canary is on edb88b9ebe, expected 7e917a4268" in errors


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


def test_core_e2e_gate_heartbeat_names_blocking_ci_job_and_step() -> None:
    def fake_fetch_run_jobs(repo: str, run_id: int) -> list[dict[str, object]]:
        if run_id == 1:
            return [
                {
                    "name": ship_monitor.DEPLOY_GATE_JOB,
                    "status": "in_progress",
                    "steps": [
                        {"name": "Wait for core E2E gate", "status": "in_progress"},
                    ],
                }
            ]
        if run_id == 2:
            return [
                {
                    "name": "Core E2E tests",
                    "status": "in_progress",
                    "steps": [
                        {"name": "Run Core E2E Tests", "status": "in_progress"},
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

    assert (
        "Deploy and Verify #1 / gate -> CI #2 / Core E2E tests / "
        "Run Core E2E Tests: in_progress"
    ) in summary


def test_deploy_heartbeat_names_active_deploy_step() -> None:
    def fake_fetch_run_jobs(repo: str, run_id: int) -> list[dict[str, object]]:
        return [
            {
                "name": ship_monitor.DEPLOY_GATE_JOB,
                "status": "completed",
                "steps": [],
            },
            {
                "name": ship_monitor.DEPLOY_DEMO_JOB,
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
        "Deploy and Verify #1 / Deploy public demo runtime / "
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


def test_runtime_image_paths_include_docker_context_rules() -> None:
    assert ".dockerignore" in ship_monitor.RUNTIME_IMAGE_PATHS
    assert "schemas" in ship_monitor.RUNTIME_IMAGE_PATHS


if __name__ == "__main__":
    test_runtime_reuse_does_not_require_exact_live_sha()
    test_runtime_reuse_accepts_deploy_stamped_target_sha()
    test_runtime_reuse_accepts_intermediate_deploy_sha()
    test_runtime_reuse_accepts_intermediate_sha_when_deploy_job_is_absent()
    test_runtime_publish_requires_exact_live_sha()
    test_runtime_publish_accepts_deploy_stamped_target_sha()
    test_live_verify_accepts_degraded_runtime_health()
    test_live_verify_retries_transient_canary_status_gap()
    test_skipped_tip_still_requires_latest_runtime_affecting_sha()
    test_gate_heartbeat_names_blocking_ci_job_and_step()
    test_core_e2e_gate_heartbeat_names_blocking_ci_job_and_step()
    test_deploy_heartbeat_names_active_deploy_step()
    test_manual_deploy_recovery_supersedes_failed_push_deploy()
    test_runtime_image_paths_include_docker_context_rules()
    print("ship-monitor tests passed")
