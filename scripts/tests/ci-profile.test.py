#!/usr/bin/env python3
"""Focused regressions for attempt-aware CI profiling.

These tests are intentionally local: no GitHub credentials or network calls are
needed.  The project gate runs this file; it is not a replacement for a live
profile recipe.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_profile", ROOT / "scripts" / "ops" / "ci-profile.py")
assert SPEC and SPEC.loader
ci_profile = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ci_profile
SPEC.loader.exec_module(ci_profile)


RUN_ID = 35111660349
BASE_RUN = {
    "id": RUN_ID,
    "run_attempt": 2,
    "workflow_name": "CI",
    "event": "push",
    "head_sha": "abc123",
    "status": "completed",
    "conclusion": "success",
    "created_at": "2026-09-16T14:53:54Z",
    "updated_at": "2026-09-16T15:29:28Z",
    "html_url": "https://github.com/cipher982/longhouse/actions/runs/35111660349",
}
ATTEMPTS = [
    {
        **BASE_RUN,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "cancelled",
        "created_at": "2026-09-16T14:53:54Z",
        "updated_at": "2026-09-16T15:09:51Z",
    },
    {
        **BASE_RUN,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-09-16T15:18:21Z",
        "run_started_at": "2026-09-16T15:18:20Z",
        "updated_at": "2026-09-16T15:29:28Z",
    },
]


def _job(job_id: int, name: str, *, conclusion: str, created: str, started: str, ended: str) -> dict[str, Any]:
    return {
        "id": job_id,
        "run_id": RUN_ID,
        "run_attempt": 1 if job_id == 1 else 2,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
        "started_at": started,
        "completed_at": ended,
        "runner_name": "Hosted Runner 1",
        "runner_id": 42,
        "runner_group_name": "Default",
        "labels": ["self-hosted", "arm64"],
        "steps": [
            {
                "number": 1,
                "name": "build",
                "status": "completed",
                "conclusion": conclusion,
                "started_at": started,
                "completed_at": ended,
            }
        ],
    }


def test_duration_does_not_turn_inversions_or_live_runs_into_zero() -> None:
    assert ci_profile.duration_seconds("2026-09-16T15:18:21Z", "2026-09-16T15:18:20Z") is None
    assert ci_profile.duration_seconds("2026-09-16T15:18:20Z", None) is None
    assert (
        ci_profile.duration_seconds(
            "2026-09-16T15:18:20Z",
            "2026-09-16T15:29:28Z",
            status="in_progress",
        )
        is None
    )


def test_rerun_profiles_keep_attempts_and_separate_lifecycle() -> None:
    payloads = {
        f"/actions/runs/{RUN_ID}": BASE_RUN,
        f"/actions/runs/{RUN_ID}/attempts/1": ATTEMPTS[0],
        f"/actions/runs/{RUN_ID}/attempts/2": ATTEMPTS[1],
        f"/actions/runs/{RUN_ID}/attempts/1/jobs?per_page=100": {
            "total_count": 1,
            "jobs": [_job(1, "test", conclusion="cancelled", created="2026-09-16T14:53:54Z", started="2026-09-16T14:54:00Z", ended="2026-09-16T15:09:51Z")],
        },
        f"/actions/runs/{RUN_ID}/attempts/2/jobs?per_page=100": {
            "total_count": 1,
            "jobs": [_job(2, "test", conclusion="success", created="2026-09-16T15:18:21Z", started="2026-09-16T15:18:20Z", ended="2026-09-16T15:29:28Z")],
        },
    }

    def fake_gh_api(repo: str, path: str) -> Any:
        return payloads[path]

    old_gh_api = ci_profile.gh_api
    ci_profile.gh_api = fake_gh_api
    profiles = ci_profile.build_profiles("cipher982/longhouse", RUN_ID, ready_job="test")
    ci_profile.gh_api = old_gh_api

    assert [profile.run_attempt for profile in profiles] == [1, 2]
    assert [profile.conclusion for profile in profiles] == ["cancelled", "success"]
    assert profiles[1].run_id == RUN_ID
    assert profiles[1].workflow_name == "CI"
    assert profiles[1].event == "push"
    assert profiles[1].head_sha == "abc123"
    assert profiles[0].attempt_duration_seconds == 957.0
    assert profiles[1].attempt_duration_seconds == 667.0
    assert profiles[0].cancelled_job_occupancy_seconds == 951.0
    assert profiles[1].job_span_seconds == 668.0
    assert profiles[1].lifecycle_duration_seconds == 2134.0
    assert profiles[1].jobs[0].queue_duration_seconds is None
    assert profiles[1].attempt_queue_duration_seconds is None
    assert profiles[1].ready_duration_seconds == 667.0
    assert profiles[1].ready_job_name == "test"
    assert profiles[1].ready_conclusion == "success"
    assert profiles[1].evidence["jobs"]["endpoint"].endswith("/attempts/2/jobs")
    ci_profile.gh_api = fake_gh_api
    selected = ci_profile.build_profiles(
        "cipher982/longhouse",
        RUN_ID,
        attempts=[1],
        ready_job="test",
    )
    ci_profile.gh_api = old_gh_api
    assert [profile.run_attempt for profile in selected] == [1]
    assert selected[0].lifecycle_duration_seconds == 2134.0

def test_jobs_are_paginated_and_runner_evidence_is_retained() -> None:

    paths = {
        "/actions/runs/1/jobs?per_page=100": {
            "total_count": 2,
            "jobs": [_job(1, "first", conclusion="success", created="2026-09-16T14:00:00Z", started="2026-09-16T14:01:00Z", ended="2026-09-16T14:02:00Z")],
        },
        "/actions/runs/1/jobs?per_page=100&page=2": {
            "total_count": 2,
            "jobs": [_job(2, "second", conclusion="failure", created="2026-09-16T14:00:00Z", started="2026-09-16T14:03:00Z", ended="2026-09-16T14:04:00Z")],
        },
    }

    old_gh_api = ci_profile.gh_api
    ci_profile.gh_api = lambda repo, path: paths[path]
    jobs, evidence = ci_profile.fetch_jobs_with_evidence("cipher982/longhouse", 1)
    ci_profile.gh_api = old_gh_api

    assert [job["name"] for job in jobs] == ["first", "second"]
    assert evidence["pages"] == [{"page": 1, "count": 1}, {"page": 2, "count": 1}]
    profiles = ci_profile._job_profiles("cipher982/longhouse", 1, 1, jobs)
    assert profiles[0].runner_labels == ["self-hosted", "arm64"]
    assert profiles[1].conclusion == "failure"

if __name__ == "__main__":
    test_duration_does_not_turn_inversions_or_live_runs_into_zero()
    test_rerun_profiles_keep_attempts_and_separate_lifecycle()
    test_jobs_are_paginated_and_runner_evidence_is_retained()
    print("ci-profile tests passed")
