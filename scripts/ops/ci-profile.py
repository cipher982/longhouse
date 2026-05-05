#!/usr/bin/env python3
"""Print deterministic GitHub Actions wall-time profiles.

This intentionally uses GitHub's jobs API instead of scraping logs. Logs are
for explaining why a step was slow; the API is the source of truth for how long
jobs and steps took.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StepProfile:
    run_id: int
    job_id: int
    job_name: str
    name: str
    status: str | None
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    duration_seconds: float | None


@dataclass(frozen=True)
class JobProfile:
    run_id: int
    job_id: int
    name: str
    status: str | None
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    duration_seconds: float | None
    steps: list[StepProfile]


@dataclass(frozen=True)
class RunProfile:
    run_id: int
    repo: str
    html_url: str | None
    head_sha: str | None
    status: str | None
    conclusion: str | None
    created_at: str | None
    updated_at: str | None
    duration_seconds: float | None
    jobs: list[JobProfile]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Profile GitHub Actions job and step wall time.")
    parser.add_argument("--repo", default="cipher982/longhouse", help="GitHub repo in OWNER/REPO form.")
    parser.add_argument(
        "--run-id",
        type=int,
        action="append",
        required=True,
        help="GitHub Actions run ID. Repeat to profile multiple runs.",
    )
    parser.add_argument("--top", type=int, default=12, help="Number of slowest steps to print. Default: 12.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args()


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "command failed")
    return proc


def gh_api(repo: str, path: str) -> Any:
    proc = run(["gh", "api", f"repos/{repo}{path}"])
    return json.loads(proc.stdout or "{}")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    start = parse_timestamp(started_at)
    completed = parse_timestamp(completed_at)
    if start is None or completed is None:
        return None
    return max(0.0, (completed - start).total_seconds())


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def fetch_jobs(repo: str, run_id: int) -> list[dict[str, Any]]:
    payload = gh_api(repo, f"/actions/runs/{run_id}/jobs?per_page=100")
    return list(payload.get("jobs") or [])


def fetch_run(repo: str, run_id: int) -> dict[str, Any]:
    return dict(gh_api(repo, f"/actions/runs/{run_id}"))


def build_profile(repo: str, run_id: int) -> RunProfile:
    run_payload = fetch_run(repo, run_id)
    job_profiles: list[JobProfile] = []
    for job in fetch_jobs(repo, run_id):
        job_id = int(job["id"])
        steps: list[StepProfile] = []
        for step in job.get("steps") or []:
            steps.append(
                StepProfile(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job.get("name") or f"job-{job_id}",
                    name=step.get("name") or f"step-{step.get('number', '?')}",
                    status=step.get("status"),
                    conclusion=step.get("conclusion"),
                    started_at=step.get("started_at"),
                    completed_at=step.get("completed_at"),
                    duration_seconds=duration_seconds(step.get("started_at"), step.get("completed_at")),
                )
            )
        job_profiles.append(
            JobProfile(
                run_id=run_id,
                job_id=job_id,
                name=job.get("name") or f"job-{job_id}",
                status=job.get("status"),
                conclusion=job.get("conclusion"),
                started_at=job.get("started_at"),
                completed_at=job.get("completed_at"),
                duration_seconds=duration_seconds(job.get("started_at"), job.get("completed_at")),
                steps=steps,
            )
        )

    return RunProfile(
        run_id=run_id,
        repo=repo,
        html_url=run_payload.get("html_url"),
        head_sha=run_payload.get("head_sha"),
        status=run_payload.get("status"),
        conclusion=run_payload.get("conclusion"),
        created_at=run_payload.get("created_at"),
        updated_at=run_payload.get("updated_at"),
        duration_seconds=duration_seconds(run_payload.get("created_at"), run_payload.get("updated_at")),
        jobs=job_profiles,
    )


def all_steps(profiles: list[RunProfile]) -> list[StepProfile]:
    steps: list[StepProfile] = []
    for profile in profiles:
        for job in profile.jobs:
            steps.extend(job.steps)
    return steps


def print_text(profiles: list[RunProfile], *, top: int) -> None:
    for profile in profiles:
        short_sha = (profile.head_sha or "")[:10] or "unknown"
        conclusion = profile.conclusion or "-"
        print(
            f"GitHub Actions wall-time profile: run {profile.run_id} "
            f"({short_sha}, {profile.status}/{conclusion}, {format_duration(profile.duration_seconds)})"
        )
        if profile.html_url:
            print(f"  {profile.html_url}")
        for job in sorted(profile.jobs, key=lambda item: item.duration_seconds or -1, reverse=True):
            conclusion = job.conclusion or "-"
            print(f"  {format_duration(job.duration_seconds):>8}  {job.name} ({job.status}/{conclusion})")
        print()

    slow_steps = sorted(
        [step for step in all_steps(profiles) if step.duration_seconds is not None],
        key=lambda item: item.duration_seconds or 0,
        reverse=True,
    )[: max(0, top)]
    if slow_steps:
        print(f"Slowest {len(slow_steps)} steps:")
        for step in slow_steps:
            conclusion = step.conclusion or "-"
            print(
                f"  {format_duration(step.duration_seconds):>8}  "
                f"run {step.run_id} / {step.job_name} / {step.name} ({step.status}/{conclusion})"
            )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    args = parse_args()
    try:
        profiles = [build_profile(args.repo, run_id) for run_id in args.run_id]
    except RuntimeError as exc:
        print(f"Failed to fetch CI profile: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump([asdict(profile) for profile in profiles], sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print_text(profiles, top=args.top)
    return 0


if __name__ == "__main__":
    os.chdir(repo_root())
    sys.exit(main())
