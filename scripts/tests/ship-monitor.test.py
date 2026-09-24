#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "ops" / "ship-monitor.py"

spec = importlib.util.spec_from_file_location("ship_monitor", MODULE_PATH)
assert spec is not None
ship_monitor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ship_monitor
spec.loader.exec_module(ship_monitor)


def deploy_status(
    demo_sha: str,
    canary_sha: str,
    *,
    demo_health: str = "healthy",
    canary_health: str = "healthy",
    canary_surface: str = ship_monitor.CANARY_SURFACE,
) -> str:
    return f"""

Surface                  SHA          Health         Build identity                  Uptime
-------                  ---          ------         -------------                  ------
Demo runtime             {demo_sha}   {demo_health}  runtime-demo-v1                 Up 2 minutes ({demo_health})
Control plane            f3e42620e7   ok             control-plane-v1                Up 2 days (healthy)
{canary_surface}         {canary_sha}   {canary_health}  runtime-canary-v1               Up 39 seconds ({canary_health})
Local HEAD               ac77b06d72

"""


def test_parse_deploy_status_normalizes_named_canary_surface() -> None:
    output = deploy_status("ac77b06d72", "ac77b06d72", canary_surface="Canary named-test-ring")

    surfaces = ship_monitor.parse_deploy_status(output)

    assert surfaces["Canary"].sha == "ac77b06d72"
    assert surfaces["Canary"].health == "healthy"


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
    deploy_status_outputs = deploy_status_output if isinstance(deploy_status_output, list) else [deploy_status_output]
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
        return [{"name": name, "conclusion": conclusion} for name, conclusion in job_conclusions.get(run_id, {}).items()]

    ship_monitor.run = fake_run
    ship_monitor.fetch_run_jobs = fake_fetch_run_jobs
    ship_monitor.latest_runtime_affecting_sha = lambda root, target_sha: latest_runtime_sha


def test_no_runtime_change_does_not_require_exact_live_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.NO_RUNTIME_CHANGE_JOB: "success"},
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


def test_no_runtime_change_accepts_deploy_stamped_target_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.NO_RUNTIME_CHANGE_JOB: "success"},
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


def test_no_runtime_change_accepts_intermediate_deploy_sha() -> None:
    with_fakes(
        {
            1: {ship_monitor.NO_RUNTIME_CHANGE_JOB: "success"},
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


def test_no_runtime_change_accepts_intermediate_sha_when_deploy_job_is_absent() -> None:
    with_fakes(
        {
            1: {ship_monitor.NO_RUNTIME_CHANGE_JOB: "success"},
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


def test_deploy_status_parses_component_identity() -> None:
    surfaces = ship_monitor.parse_deploy_status(deploy_status("a" * 10, "b" * 10))

    assert surfaces["Control plane"].sha == "f3e42620e7"
    assert surfaces["Control plane"].build_identity == "control-plane-v1"
    assert surfaces[ship_monitor.CANARY_SURFACE].sha == "bbbbbbbbbb"
    assert surfaces[ship_monitor.CANARY_SURFACE].build_identity == "runtime-canary-v1"


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
    assert f"{ship_monitor.CANARY_SURFACE} is on latest, expected ac77b06d72" in errors


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
                deploy_status("5329d01c9b", "-"),
                deploy_status("5329d01c9b", "5329d01c9b"),
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


def test_no_runtime_change_reports_explicit_disposition_without_live_sha_requirement() -> None:
    with_fakes(
        {
            1: {ship_monitor.NO_RUNTIME_CHANGE_JOB: "success"},
        },
        latest_runtime_sha="7e917a42689f626ed83908f7ab0a6ab21c3aafc4",
        deploy_status_output=deploy_status("edb88b9ebe", "edb88b9ebe"),
    )
    runs = [
        run_info(ship_monitor.DEPLOY_AND_VERIFY, 1),
    ]

    _surfaces, errors, _raw = ship_monitor.verify_live_state(
        ROOT,
        "cipher982/longhouse",
        "7ede50e79d",
        runs,
    )

    assert errors == []


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


def test_deploy_gate_heartbeat_names_blocking_ci_job_and_step() -> None:
    # Both the pre-2026-09-24 step name and the current one must resolve to the
    # blocking CI job: historical runs still carry the old name.
    for gate_step_name in (
        "Wait for core E2E gate",
        "Wait for the suites that can invalidate this deploy",
    ):
        _assert_gate_heartbeat_names_blocking_job(gate_step_name)


def _assert_gate_heartbeat_names_blocking_job(gate_step_name: str) -> None:
    def fake_fetch_run_jobs(repo: str, run_id: int) -> list[dict[str, object]]:
        if run_id == 1:
            return [
                {
                    "name": ship_monitor.DEPLOY_GATE_JOB,
                    "status": "in_progress",
                    "steps": [
                        {"name": gate_step_name, "status": "in_progress"},
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

    assert ("Deploy and Verify #1 / gate -> CI #2 / Core E2E tests / Run Core E2E Tests: in_progress") in summary


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

    assert ("Deploy and Verify #1 / Deploy public demo runtime / Deploy public demo runtime: in_progress") in summary


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


def test_runs_on_a_topic_branch_do_not_speak_for_the_ship() -> None:
    """The same SHA often exists on a topic branch and on main; only main ships.

    A topic-branch run of the same commit failed on a git fetch network timeout
    while main's own run was green, and the monitor read the branch run as the
    ship's verdict. Prefer the default branch's runs when it has any.
    """

    payload = [
        {
            "databaseId": 1,
            "workflowName": "CI",
            "status": "completed",
            "conclusion": "success",
            "url": "",
            "headSha": "abc123",
            "createdAt": "",
            "event": "push",
            "headBranch": "main",
        },
        {
            "databaseId": 2,
            "workflowName": "CI",
            "status": "completed",
            "conclusion": "failure",
            "url": "",
            "headSha": "abc123",
            "createdAt": "",
            "event": "push",
            "headBranch": "ci-gate-shape",
        },
    ]

    def fake_run(cmd, cwd=None, check=True, env=None):  # noqa: ANN001
        stdout = "origin/main\n" if cmd[:2] == ["git", "symbolic-ref"] else json.dumps(payload)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    previous = ship_monitor.run
    ship_monitor.run = fake_run
    try:
        runs = ship_monitor.fetch_runs("cipher982/longhouse", "abc123")
    finally:
        ship_monitor.run = previous

    assert [run.databaseId for run in runs] == [1]


def test_runtime_schema_only_change_requires_new_runtime() -> None:
    with tempfile.TemporaryDirectory(prefix="longhouse-runtime-paths-") as temp:
        root = Path(temp)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        paths = (
            root / "scripts" / "ops" / "runtime-schema.py",
            root / "scripts" / "ops" / "release-artifacts.py",
        )
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("before\n")
        subprocess.run(["git", "add", "--", "scripts/ops/runtime-schema.py", "scripts/ops/release-artifacts.py"], cwd=root, check=True)
        commit = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"]
        subprocess.run(commit, cwd=root, check=True)
        for path in paths:
            path.write_text("after\n")
        subprocess.run(["git", "add", "--", "scripts/ops/runtime-schema.py", "scripts/ops/release-artifacts.py"], cwd=root, check=True)
        subprocess.run(commit[:-1] + ["runtime inputs changed"], cwd=root, check=True)
        target_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        assert ship_monitor.latest_runtime_affecting_sha(root, target_sha) == target_sha


def _run(name: str, conclusion: str | None, status: str = "completed", run_id: int = 1) -> "ship_monitor.RunInfo":
    return ship_monitor.RunInfo(databaseId=run_id, workflowName=name, status=status, conclusion=conclusion, url="u")


def test_superseded_run_follows_the_main_head_that_contains_it() -> None:
    # A dropped queued run is not a failure of the pushed commit: main's newer
    # head contains it, and that head's deploy ships the change.
    old, head = "a" * 40, "b" * 40
    runs_by_sha = {
        old: [_run("CI", "cancelled"), _run("Deploy and Verify", "success", run_id=2)],
        head: [_run("CI", "success", run_id=3), _run("Deploy and Verify", "success", run_id=4)],
    }
    saved = (ship_monitor.wait_for_workflows, ship_monitor.fetch_remote_head, ship_monitor.contains_commit, ship_monitor.fetch_run_jobs)
    ship_monitor.wait_for_workflows = lambda args, sha: runs_by_sha[sha]
    ship_monitor.fetch_remote_head = lambda repo, branch="main": head
    ship_monitor.contains_commit = lambda root, descendant, ancestor: (descendant, ancestor) == (head, old)
    ship_monitor.fetch_run_jobs = lambda repo, run_id: []
    try:
        sha, runs = ship_monitor.wait_following_coverage(type("A", (), {"repo": "r"})(), Path("."), old)
    finally:
        ship_monitor.wait_for_workflows, ship_monitor.fetch_remote_head, ship_monitor.contains_commit, ship_monitor.fetch_run_jobs = saved
    assert sha == head
    assert runs == runs_by_sha[head]


def test_a_real_failure_on_main_head_is_not_treated_as_superseded() -> None:
    head = "c" * 40
    runs = [_run("CI", "failure")]
    saved = (ship_monitor.wait_for_workflows, ship_monitor.fetch_remote_head)
    ship_monitor.wait_for_workflows = lambda args, sha: runs
    ship_monitor.fetch_remote_head = lambda repo, branch="main": head
    try:
        sha, got = ship_monitor.wait_following_coverage(type("A", (), {"repo": "r"})(), Path("."), head)
    finally:
        ship_monitor.wait_for_workflows, ship_monitor.fetch_remote_head = saved
    assert (sha, got) == (head, runs)


def test_deploy_that_stood_down_counts_as_superseded() -> None:
    jobs = [
        {"name": ship_monitor.GATE_JOB, "conclusion": "success"},
        {"name": ship_monitor.CANARY_REPROVISION_JOB, "conclusion": "skipped"},
    ]
    saved = ship_monitor.fetch_run_jobs
    ship_monitor.fetch_run_jobs = lambda repo, run_id: jobs
    try:
        assert ship_monitor.was_superseded("r", [_run(ship_monitor.DEPLOY_AND_VERIFY, "success")])
        jobs.append({"name": ship_monitor.NO_RUNTIME_CHANGE_JOB, "conclusion": "success"})
        assert not ship_monitor.was_superseded("r", [_run(ship_monitor.DEPLOY_AND_VERIFY, "success")])
    finally:
        ship_monitor.fetch_run_jobs = saved


if __name__ == "__main__":
    test_runtime_schema_only_change_requires_new_runtime()
    test_no_runtime_change_does_not_require_exact_live_sha()
    test_no_runtime_change_accepts_deploy_stamped_target_sha()
    test_no_runtime_change_accepts_intermediate_deploy_sha()
    test_no_runtime_change_accepts_intermediate_sha_when_deploy_job_is_absent()
    test_runtime_publish_requires_exact_live_sha()
    test_deploy_status_parses_component_identity()
    test_runtime_publish_accepts_deploy_stamped_target_sha()
    test_live_verify_accepts_degraded_runtime_health()
    test_live_verify_retries_transient_canary_status_gap()
    test_no_runtime_change_reports_explicit_disposition_without_live_sha_requirement()
    test_gate_heartbeat_names_blocking_ci_job_and_step()
    test_deploy_gate_heartbeat_names_blocking_ci_job_and_step()
    test_runs_on_a_topic_branch_do_not_speak_for_the_ship()
    test_deploy_heartbeat_names_active_deploy_step()
    test_manual_deploy_recovery_supersedes_failed_push_deploy()
    test_superseded_run_follows_the_main_head_that_contains_it()
    test_a_real_failure_on_main_head_is_not_treated_as_superseded()
    test_deploy_that_stood_down_counts_as_superseded()
    print("ship-monitor tests passed")
