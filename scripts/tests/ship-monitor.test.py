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

    assert surfaces[ship_monitor.CANARY_SURFACE].sha == "ac77b06d72"
    assert surfaces[ship_monitor.CANARY_SURFACE].health == "healthy"


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


def with_fakes(job_conclusions: dict[int, dict[str, str]], *, deploy_status_output: str | list[str] | None = None,
               contains: bool = False) -> None:
    outputs = deploy_status_output if isinstance(deploy_status_output, list) else [deploy_status_output]
    index = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal index
        output = outputs[min(index, len(outputs) - 1)]
        index += 1
        if output is None:
            output = deploy_status("latest", "latest")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    ship_monitor.run = fake_run
    ship_monitor.fetch_run_jobs = lambda repo, run_id: [
        {"name": name, "conclusion": conclusion} for name, conclusion in job_conclusions.get(run_id, {}).items()
    ]
    ship_monitor.contains_commit = lambda root, descendant, ancestor: contains


SHA = "ac77b06d72" + "0" * 30
RELEASE = ship_monitor.RELEASE_WORKFLOW


def test_a_release_that_deployed_requires_the_canary_on_the_commit() -> None:
    with_fakes({1: {ship_monitor.RELEASE_CANARY_JOB: "success"}})
    _surfaces, errors, _raw = ship_monitor.verify_live_state(ROOT, "r", SHA, [run_info(RELEASE, 1, event="workflow_run")])
    assert errors == [f"{ship_monitor.CANARY_SURFACE} is on latest, expected ac77b06d72 or a commit that contains it"]


def test_a_canary_on_a_descendant_or_the_commit_passes() -> None:
    with_fakes({1: {ship_monitor.RELEASE_CANARY_JOB: "success"}}, deploy_status_output=deploy_status("x", "ac77b06d72"))
    assert ship_monitor.verify_live_state(ROOT, "r", SHA, [run_info(RELEASE, 1)])[1] == []
    with_fakes({1: {ship_monitor.RELEASE_CANARY_JOB: "success"}}, deploy_status_output=deploy_status("x", "ffffffffff"), contains=True)
    assert ship_monitor.verify_live_state(ROOT, "r", SHA, [run_info(RELEASE, 1)])[1] == []


def test_a_release_with_nothing_to_deploy_does_not_require_the_live_sha() -> None:
    with_fakes({1: {"Plan": "success", ship_monitor.RELEASE_CANARY_JOB: "skipped"}})
    runs = [run_info(RELEASE, 1)]
    assert ship_monitor.verify_live_state(ROOT, "r", SHA, runs)[1] == []
    assert ship_monitor.release_disposition("r", runs) == "no_runtime_change"
    assert ship_monitor.release_disposition("r", [run_info("CI", 2)]) == "unverified"


def test_stale_demo_sha_does_not_block_ship() -> None:
    with_fakes({1: {ship_monitor.RELEASE_CANARY_JOB: "success"}}, deploy_status_output=deploy_status("stale0sha00", "ac77b06d72"))
    surfaces, errors, raw = ship_monitor.verify_live_state(ROOT, "r", SHA, [run_info(RELEASE, 1)])
    assert errors == []
    assert surfaces["Demo runtime"].sha == "stale0sha00" and "Demo runtime" in raw


def test_live_verify_accepts_degraded_runtime_health() -> None:
    with_fakes(
        {1: {ship_monitor.RELEASE_CANARY_JOB: "success"}},
        deploy_status_output=deploy_status("ac77b06d72", "ac77b06d72", canary_health="degraded"),
    )
    assert ship_monitor.verify_live_state(ROOT, "r", SHA, [run_info(RELEASE, 1)])[1] == []


def test_live_verify_retries_transient_canary_status_gap() -> None:
    original_sleep = ship_monitor.time.sleep
    sleeps: list[float] = []
    ship_monitor.time.sleep = lambda seconds: sleeps.append(seconds)
    try:
        with_fakes(
            {1: {ship_monitor.RELEASE_CANARY_JOB: "success"}},
            deploy_status_output=[deploy_status("5329d01c9b", "-"), deploy_status("5329d01c9b", "5329d01c9b")],
        )
        _s, errors, _r = ship_monitor.verify_live_state(ROOT, "r", "5329d01c9b5265189df9164a06b128bb47df8482", [run_info(RELEASE, 1)])
        assert errors == [] and sleeps == [2]
    finally:
        ship_monitor.time.sleep = original_sleep


def test_deploy_status_parses_component_identity() -> None:
    surfaces = ship_monitor.parse_deploy_status(deploy_status("a" * 10, "b" * 10))
    assert surfaces["Control plane"].sha == "f3e42620e7"
    assert surfaces["Control plane"].build_identity == "control-plane-v1"
    assert surfaces[ship_monitor.CANARY_SURFACE].sha == "bbbbbbbbbb"
    assert surfaces[ship_monitor.CANARY_SURFACE].build_identity == "runtime-canary-v1"


def test_a_release_run_belongs_to_the_commit_in_its_title() -> None:
    other = "d" * 40
    assert ship_monitor.release_sha({"display_title": f"Release {SHA}", "head_sha": other}) == SHA
    assert ship_monitor.release_sha({"display_title": "Release", "head_sha": other}) == other


def test_ci_and_release_speak_for_the_ship_and_a_passed_ci_waits_for_its_release() -> None:
    runs = [run_info("CI", 1), run_info("Terminal Truth", 2, conclusion="failure"), run_info("Launch Gate", 3, status="in_progress", conclusion=None)]
    selected, names = ship_monitor.select_load_bearing_runs(runs)
    assert names == ["CI"] and [r.databaseId for r in selected] == [1]
    assert ship_monitor.awaiting_release(runs)
    runs.append(run_info(RELEASE, 4, status="in_progress", conclusion=None, event="workflow_run"))
    selected, names = ship_monitor.select_load_bearing_runs(runs)
    assert names == ["CI", RELEASE] and not ship_monitor.awaiting_release(runs)
    assert not ship_monitor.awaiting_release([run_info("CI", 1, conclusion="failure")])
    assert not ship_monitor.awaiting_release([run_info("CI", 1, status="in_progress", conclusion=None)])


def test_a_manual_release_rerun_supersedes_a_failed_one() -> None:
    runs = [run_info(RELEASE, 1, conclusion="failure"), run_info("CI", 2), run_info(RELEASE, 3, event="workflow_dispatch")]
    selected, names = ship_monitor.select_load_bearing_runs(runs)
    assert names == ["CI", RELEASE]
    assert [r.databaseId for r in selected] == [2, 3] and ship_monitor.runs_succeeded(selected)


def test_runs_on_a_topic_branch_do_not_speak_for_the_ship() -> None:
    """The same SHA often exists on a topic branch and on main; only main ships."""

    def item(run_id: int, branch: str, conclusion: str) -> dict:
        return {
            "id": run_id, "name": "CI", "status": "completed", "conclusion": conclusion, "html_url": "",
            "head_sha": "abc123", "created_at": "", "event": "push", "head_branch": branch,
        }

    payload = {"workflow_runs": [item(1, "main", "success"), item(2, "ci-gate-shape", "failure")]}
    ship_monitor._ETAG_CACHE.clear()

    def fake_run(cmd, cwd=None, check=True, env=None):  # noqa: ANN001
        if cmd[:2] == ["git", "symbolic-ref"]:
            stdout = "origin/main\n"
        else:
            stdout = 'HTTP/2.0 200 OK\r\nEtag: "e1"\r\n\r\n' + json.dumps(payload)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    previous = ship_monitor.run
    ship_monitor.run = fake_run
    try:
        runs = ship_monitor.fetch_runs("cipher982/longhouse", "abc123")
    finally:
        ship_monitor.run = previous

    assert [run.databaseId for run in runs] == [1]


def test_release_runs_are_found_by_title_among_the_workflow_runs() -> None:
    ci = {"id": 1, "name": "CI", "status": "completed", "conclusion": "success", "html_url": "", "head_sha": SHA,
          "created_at": "", "event": "push", "head_branch": "main"}
    # The shape GitHub serves: a run's `name` is its run-name, not the workflow's (release.yml run-name).
    mine = {"id": 5, "name": f"Release {SHA}", "status": "in_progress", "conclusion": None, "html_url": "",
            "head_sha": "e" * 40, "display_title": f"Release {SHA}", "path": ".github/workflows/release.yml",
            "created_at": "", "event": "workflow_run", "head_branch": "main"}
    theirs = {**mine, "id": 6, "name": "Release " + "f" * 40, "display_title": "Release " + "f" * 40}
    ship_monitor._ETAG_CACHE.clear()

    def fake_run(cmd, cwd=None, check=True, env=None):  # noqa: ANN001
        if cmd[:2] == ["git", "symbolic-ref"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="origin/main\n", stderr="")
        body = {"workflow_runs": [mine, theirs]} if "release.yml" in cmd[-1] else {"workflow_runs": [ci]}
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='HTTP/2.0 200 OK\r\n\r\n' + json.dumps(body), stderr="")

    previous = ship_monitor.run
    ship_monitor.run = fake_run
    try:
        runs = ship_monitor.fetch_runs("cipher982/longhouse", SHA)
    finally:
        ship_monitor.run = previous
    assert [(r.workflowName, r.databaseId) for r in runs] == [("CI", 1), ("Release", 5)]


def test_unchanged_run_list_is_a_conditional_request_answered_by_304() -> None:
    """A 304 is free against the GitHub rate limit; one call per list per poll, with If-None-Match after the first."""

    ship_monitor._ETAG_CACHE.clear()
    calls: list[list[str]] = []
    body = {"workflow_runs": [{"id": 7, "name": "CI", "status": "in_progress", "conclusion": None, "html_url": "",
                               "head_sha": "abc123", "created_at": "", "event": "push", "head_branch": "main"}]}

    def fake_run(cmd, cwd=None, check=True, env=None):  # noqa: ANN001
        if cmd[:2] == ["git", "symbolic-ref"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="origin/main\n", stderr="")
        calls.append(cmd)
        if any(part.startswith("If-None-Match") for part in cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="HTTP/2.0 304 Not Modified\r\n\r\n", stderr="")
        etag = "e" + str(len(calls))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=f'HTTP/2.0 200 OK\r\nEtag: "{etag}"\r\n\r\n' + json.dumps(body), stderr=""
        )

    previous = ship_monitor.run
    ship_monitor.run = fake_run
    try:
        first = ship_monitor.fetch_runs("cipher982/longhouse", "abc123")
        second = ship_monitor.fetch_runs("cipher982/longhouse", "abc123")
    finally:
        ship_monitor.run = previous

    assert len(calls) == 4, calls  # the commit's runs and the Release list, once per poll each
    assert sum("If-None-Match" in " ".join(c) for c in calls[:2]) == 0 and sum("If-None-Match" in " ".join(c) for c in calls[2:]) == 2
    assert [r.databaseId for r in first] == [r.databaseId for r in second] == [7]


def _run(name: str, conclusion: str | None, status: str = "completed", run_id: int = 1) -> "ship_monitor.RunInfo":
    return ship_monitor.RunInfo(databaseId=run_id, workflowName=name, status=status, conclusion=conclusion, url="u")


def test_superseded_run_follows_the_main_head_that_contains_it() -> None:
    # A dropped queued run is not a failure of the pushed commit: main's newer head contains it, and that
    # head's Release deploys everything since what the tenant serves.
    old, head = "a" * 40, "b" * 40
    runs_by_sha = {
        old: [_run("CI", "cancelled")],
        head: [_run("CI", "success", run_id=3), _run("Release", "success", run_id=4)],
    }
    saved = (ship_monitor.wait_for_workflows, ship_monitor.fetch_remote_head, ship_monitor.contains_commit)
    ship_monitor.wait_for_workflows = lambda args, sha: runs_by_sha[sha]
    ship_monitor.fetch_remote_head = lambda repo, branch="main": head
    ship_monitor.contains_commit = lambda root, descendant, ancestor: (descendant, ancestor) == (head, old)
    try:
        sha, runs = ship_monitor.wait_following_coverage(type("A", (), {"repo": "r"})(), Path("."), old)
    finally:
        ship_monitor.wait_for_workflows, ship_monitor.fetch_remote_head, ship_monitor.contains_commit = saved
    assert sha == head
    assert runs == runs_by_sha[head]


def test_a_dropped_pending_release_is_a_supersession() -> None:
    assert ship_monitor.was_superseded([_run("CI", "success"), _run("Release", "cancelled")])
    assert not ship_monitor.was_superseded([_run("CI", "success"), _run("Release", "success")])


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


def test_the_heartbeat_names_the_running_release_job_and_the_wait_for_it() -> None:
    saved = ship_monitor.fetch_run_jobs
    ship_monitor.fetch_run_jobs = lambda repo, run_id: [
        {"name": "Plan", "status": "completed", "conclusion": "success"},
        {"name": "Hosted Live QA", "status": "in_progress", "steps": [{"name": "Run hosted live QA", "status": "in_progress"}]},
    ]
    try:
        text = ship_monitor.summarize_incomplete_runs("r", SHA, [_run("CI", "success"), _run("Release", None, "in_progress", 9)])
        assert "Release #9 / Hosted Live QA / Run hosted live QA" in text
        assert "waiting for the Release run" in ship_monitor.summarize_incomplete_runs("r", SHA, [_run("CI", "success")])
    finally:
        ship_monitor.fetch_run_jobs = saved


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ship-monitor tests passed")
