#!/usr/bin/env python3
"""Print attempt-aware GitHub Actions wall-time profiles.

GitHub exposes a workflow run and each rerun attempt as separate timing
surfaces.  This profiler keeps those surfaces separate: the run ID and
attempt identify one profile, while lifecycle fields describe the original
run through its reruns.  Jobs are read from the attempt-specific API and are
paginated rather than assuming that the first page is complete.

This intentionally uses GitHub's jobs API instead of scraping logs.  Logs are
for explaining why a step was slow; the API is the source of truth for how
long jobs and steps took.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


PER_PAGE = 100
_FAILED_JOB_OUTCOMES = {"action_required", "failure", "startup_failure", "timed_out"}


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
    run_attempt: int = 1
    number: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


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
    run_attempt: int = 1
    created_at: str | None = None
    runner_name: str | None = None
    runner_id: int | None = None
    runner_group_name: str | None = None
    runner_group_id: int | None = None
    runner_labels: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    queue_duration_seconds: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


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
    run_attempt: int = 1
    workflow_name: str | None = None
    event: str | None = None
    completed_at: str | None = None
    lifecycle_created_at: str | None = None
    lifecycle_completed_at: str | None = None
    lifecycle_duration_seconds: float | None = None
    attempt_duration_seconds: float | None = None
    ready_job: str | None = None
    ready_job_name: str | None = None
    ready_at: str | None = None
    ready_conclusion: str | None = None
    ready_duration_seconds: float | None = None
    job_span_seconds: float | None = None
    job_occupancy_seconds: float | None = None
    cancelled_job_occupancy_seconds: float | None = None
    failed_job_occupancy_seconds: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    workflow_id: int | None = None
    run_started_at: str | None = None
    attempt_queue_duration_seconds: float | None = None
    ready_job_duration_seconds: float | None = None


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
    parser.add_argument(
        "--attempt",
        "--run-attempt",
        dest="attempt",
        type=int,
        action="append",
        default=None,
        help="Only profile this attempt number. Repeat for multiple attempts; default is the complete rerun history.",
    )
    parser.add_argument("--top", type=int, default=12, help="Number of slowest steps to print. Default: 12.")
    parser.add_argument(
        "--exclude-job",
        action="append",
        default=[],
        help="Job name to omit from output and aggregate metrics. Repeatable.",
    )
    parser.add_argument(
        "--ready-job",
        "--ready-job-name",
        dest="ready_job",
        default=None,
        help="Named job whose completion is the ready milestone; never inferred from ship success.",
    )
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
    except (TypeError, ValueError):
        return None


def duration_seconds(
    started_at: str | None,
    completed_at: str | None,
    *,
    status: str | None = None,
) -> float | None:
    """Return elapsed time only for a known, completed, ordered interval."""

    if status and status.lower() in {"queued", "in_progress", "pending", "requested", "waiting"}:
        return None
    start = parse_timestamp(started_at)
    completed = parse_timestamp(completed_at)
    if start is None or completed is None or completed < start:
        return None
    return (completed - start).total_seconds()


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def _safe_fields(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Keep JSON evidence to identifiers, labels, URLs, and timing fields."""

    return {key: payload[key] for key in keys if key in payload}


def _page_path(endpoint: str, page: int) -> str:
    suffix = f"per_page={PER_PAGE}"
    if page > 1:
        suffix += f"&page={page}"
    return f"{endpoint}?{suffix}"


def _paginate(
    repo: str,
    endpoint: str,
    item_key: str | tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    pages: list[dict[str, int]] = []
    total_count: int | None = None
    keys = (item_key,) if isinstance(item_key, str) else item_key
    page = 1
    while True:
        payload = gh_api(repo, _page_path(endpoint, page))
        if isinstance(payload, list):
            page_items = [item for item in payload if isinstance(item, dict)]
        else:
            page_items = []
            for key in keys:
                if (payload or {}).get(key) is not None:
                    page_items = list((payload or {}).get(key) or [])
                    break
            raw_total = (payload or {}).get("total_count")
            if isinstance(raw_total, int):
                total_count = raw_total
        items.extend(item for item in page_items if isinstance(item, dict))
        pages.append({"page": page, "count": len(page_items)})
        if not page_items:
            break
        if total_count is not None and len(items) >= total_count:
            break
        if total_count is None and len(page_items) < PER_PAGE:
            break
        page += 1
        # A GitHub response cannot legitimately need this many pages.  This
        # is a guard for a broken/mock endpoint that repeats a full page.
        if page > 10000:
            raise RuntimeError(f"pagination did not terminate for {endpoint}")
    return items, {"endpoint": endpoint, "pages": pages, "total_count": total_count, "returned": len(items)}


def fetch_jobs_with_evidence(
    repo: str,
    run_id: int,
    run_attempt: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if run_attempt is None:
        endpoint = f"/actions/runs/{run_id}/jobs"
    else:
        endpoint = f"/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
    return _paginate(repo, endpoint, "jobs")


def fetch_jobs(repo: str, run_id: int, run_attempt: int | None = None) -> list[dict[str, Any]]:
    """Fetch every job for a run or a specific rerun attempt."""

    jobs, _ = fetch_jobs_with_evidence(repo, run_id, run_attempt)
    return jobs


def fetch_run(repo: str, run_id: int) -> dict[str, Any]:
    return dict(gh_api(repo, f"/actions/runs/{run_id}"))


def fetch_attempts_with_evidence(
    repo: str,
    run_id: int,
    latest_payload: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch the latest run and every prior attempt.

    GitHub exposes a point endpoint for attempts, not a collection endpoint.
    Attempt numbers are contiguous, so the latest ``run_attempt`` lets us
    retrieve the complete chain while keeping each response isolated.
    """

    latest = latest_payload if latest_payload is not None else fetch_run(repo, run_id)
    latest_number = _attempt_number(latest)
    attempts: list[dict[str, Any]] = []
    fetched: list[dict[str, int]] = []
    for number in range(1, latest_number + 1):
        payload = gh_api(
            repo,
            f"/actions/runs/{run_id}/attempts/{number}",
        )
        if not payload.get("run_attempt"):
            payload = {**payload, "run_attempt": number}
        attempts.append(dict(payload))
        fetched.append({"attempt": number, "count": 1})
    return attempts, {
        "endpoint": f"/actions/runs/{run_id}/attempts/{{attempt}}",
        "attempts": fetched,
        "returned": len(attempts),
        "available": True,
    }


def fetch_attempts(repo: str, run_id: int) -> list[dict[str, Any]]:
    attempts, _ = fetch_attempts_with_evidence(repo, run_id)
    return attempts


def _attempt_number(payload: dict[str, Any], default: int = 1) -> int:
    value = payload.get("run_attempt", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _terminal_at(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {
        "queued",
        "in_progress",
        "pending",
        "requested",
        "waiting",
    }:
        return None
    completed_at = payload.get("completed_at")
    if completed_at:
        return completed_at
    # Workflow run objects expose updated_at rather than completed_at.  It is
    # terminal evidence only after GitHub says the run is completed.
    if status == "completed":
        return payload.get("updated_at")
    return None


def _job_profiles(
    repo: str,
    run_id: int,
    run_attempt: int,
    jobs: list[dict[str, Any]],
) -> list[JobProfile]:
    job_profiles: list[JobProfile] = []
    for job in jobs:
        try:
            job_id = int(job["id"])
        except (KeyError, TypeError, ValueError):
            continue
        job_name = job.get("name") or f"job-{job_id}"
        steps: list[StepProfile] = []
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            steps.append(
                StepProfile(
                    run_id=run_id,
                    job_id=job_id,
                    job_name=job_name,
                    name=step.get("name") or f"step-{step.get('number', '?')}",
                    status=step.get("status"),
                    conclusion=step.get("conclusion"),
                    started_at=step.get("started_at"),
                    completed_at=step.get("completed_at"),
                    duration_seconds=duration_seconds(
                        step.get("started_at"),
                        step.get("completed_at"),
                        status=step.get("status"),
                    ),
                    run_attempt=run_attempt,
                    number=step.get("number"),
                    evidence=_safe_fields(
                        step,
                        (
                            "number",
                            "name",
                            "status",
                            "conclusion",
                            "started_at",
                            "completed_at",
                        ),
                    ),
                )
            )
        labels = job.get("labels") or []
        runner_labels = [label for label in labels if isinstance(label, str)]
        started_at = job.get("started_at")
        completed_at = job.get("completed_at")
        status = job.get("status")
        job_profiles.append(
            JobProfile(
                run_id=run_id,
                job_id=job_id,
                name=job_name,
                status=status,
                conclusion=job.get("conclusion"),
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration_seconds(started_at, completed_at, status=status),
                steps=steps,
                run_attempt=run_attempt,
                created_at=job.get("created_at"),
                runner_name=job.get("runner_name"),
                runner_id=job.get("runner_id"),
                runner_group_name=job.get("runner_group_name"),
                runner_group_id=job.get("runner_group_id"),
                runner_labels=runner_labels,
                labels=runner_labels,
                queue_duration_seconds=duration_seconds(job.get("created_at"), started_at, status=status),
                evidence=_safe_fields(
                    job,
                    (
                        "id",
                        "run_id",
                        "run_attempt",
                        "name",
                        "status",
                        "conclusion",
                        "created_at",
                        "started_at",
                        "completed_at",
                        "runner_name",
                        "runner_id",
                        "runner_group_id",
                        "runner_group_name",
                        "labels",
                        "html_url",
                        "url",
                    ),
                ),
            )
        )
    return job_profiles

def _aggregate_jobs(
    jobs: list[JobProfile],
    ready_job: str | None,
    source_created_at: str | None = None,
) -> dict[str, Any]:
    durations = [job.duration_seconds for job in jobs]
    occupancy = None if not jobs or any(value is None for value in durations) else sum(durations)
    cancelled = [job for job in jobs if (job.conclusion or "").lower() == "cancelled"]
    failed = [job for job in jobs if (job.conclusion or "").lower() in _FAILED_JOB_OUTCOMES]

    def outcome_occupancy(selected: list[JobProfile]) -> float | None:
        if not selected:
            return 0.0
        values = [job.duration_seconds for job in selected]
        return None if any(value is None for value in values) else sum(values)

    intervals = [
        (parse_timestamp(job.started_at), parse_timestamp(job.completed_at))
        for job in jobs
    ]
    job_span: float | None
    if not jobs or any(job.duration_seconds is None for job in jobs):
        job_span = None
    else:
        job_span = (
            max(end for _, end in intervals) - min(start for start, _ in intervals)
        ).total_seconds()

    ready = next((job for job in jobs if ready_job is not None and job.name == ready_job), None)
    ready_job_duration = (
        duration_seconds(
            ready.created_at,
            ready.completed_at,
            status=ready.status,
        )
        if ready
        else None
    )
    ready_elapsed = (
        duration_seconds(
            source_created_at,
            ready.completed_at,
            status=ready.status,
        )
        if ready and ready.conclusion == "success"
        else None
    )
    return {
        "ready_job_name": ready.name if ready else None,
        "ready_at": ready.completed_at if ready and ready_elapsed is not None else None,
        "ready_conclusion": ready.conclusion if ready else None,
        "ready_duration_seconds": ready_elapsed,
        "ready_job_duration_seconds": ready_job_duration,
        "job_span_seconds": job_span,
        "job_occupancy_seconds": occupancy,
        "cancelled_job_occupancy_seconds": outcome_occupancy(cancelled),
        "failed_job_occupancy_seconds": outcome_occupancy(failed),
    }


def build_profiles(
    repo: str,
    run_id: int,
    *,
    attempts: list[int] | None = None,
    ready_job: str | None = None,
) -> list[RunProfile]:
    """Build one isolated profile per attempt, retaining full lifecycle evidence."""

    run_payload = fetch_run(repo, run_id)
    attempt_payloads, history_evidence = fetch_attempts_with_evidence(
        repo,
        run_id,
        latest_payload=run_payload,
    )
    if not attempt_payloads:
        raise RuntimeError(f"run {run_id} returned no attempt history")
    by_attempt: dict[int, dict[str, Any]] = {}
    for payload in attempt_payloads:
        number = _attempt_number(payload, _attempt_number(run_payload))
        merged = {**payload, "run_attempt": number}
        for key in ("id", "workflow_id", "head_sha", "event", "workflow_name", "name", "html_url"):
            if not merged.get(key):
                merged[key] = run_payload.get(key)
        by_attempt[number] = merged
    requested = sorted(set(attempts)) if attempts is not None else sorted(by_attempt)
    if not requested:
        requested = sorted(by_attempt)
    missing = [number for number in requested if number not in by_attempt]
    if missing:
        raise ValueError(f"run {run_id} has no attempt(s): {', '.join(map(str, missing))}")

    lifecycle_records = [by_attempt[number] for number in sorted(by_attempt)]
    lifecycle_created_dt = parse_timestamp(lifecycle_records[0].get("created_at"))
    latest_record = lifecycle_records[-1]
    lifecycle_completed = _terminal_at(latest_record)
    lifecycle_duration = duration_seconds(
        lifecycle_created_dt.isoformat() if lifecycle_created_dt else None,
        lifecycle_completed,
        status=latest_record.get("status"),
    )
    known_lifecycle_completed = lifecycle_completed if lifecycle_duration is not None else None
    profiles: list[RunProfile] = []
    for number in requested:
        attempt = by_attempt[number]
        jobs, jobs_evidence = fetch_jobs_with_evidence(
            repo,
            run_id,
            number,
        )
        job_profiles = _job_profiles(repo, run_id, number, jobs)
        terminal_at = _terminal_at(attempt)
        attempt_duration = duration_seconds(
            attempt.get("created_at"),
            terminal_at,
            status=attempt.get("status"),
        )
        known_terminal_at = terminal_at if attempt_duration is not None else None
        metrics = _aggregate_jobs(job_profiles, ready_job, attempt.get("created_at"))
        attempt_evidence = {
            "run": _safe_fields(
                attempt,
                (
                    "id",
                    "run_attempt",
                    "workflow_id",
                    "workflow_name",
                    "name",
                    "event",
                    "head_sha",
                    "status",
                    "conclusion",
                    "created_at",
                    "run_started_at",
                    "updated_at",
                    "completed_at",
                    "previous_attempt_url",
                    "html_url",
                ),
            ),
            "attempts": history_evidence,
            "jobs": jobs_evidence,
            "ready_job": ready_job,
        }
        profiles.append(
            RunProfile(
                run_id=run_id,
                repo=repo,
                html_url=attempt.get("html_url"),
                head_sha=attempt.get("head_sha"),
                status=attempt.get("status"),
                conclusion=attempt.get("conclusion"),
                created_at=attempt.get("created_at"),
                updated_at=attempt.get("updated_at"),
                duration_seconds=attempt_duration,
                jobs=job_profiles,
                run_attempt=number,
                workflow_name=attempt.get("workflow_name") or attempt.get("name"),
                event=attempt.get("event"),
                workflow_id=attempt.get("workflow_id"),
                run_started_at=attempt.get("run_started_at"),
                attempt_queue_duration_seconds=duration_seconds(
                    attempt.get("created_at"),
                    attempt.get("run_started_at"),
                    status=attempt.get("status"),
                ),
                completed_at=known_terminal_at,
                lifecycle_created_at=(
                    lifecycle_created_dt.isoformat() if lifecycle_created_dt else None
                ),
                lifecycle_completed_at=known_lifecycle_completed,
                lifecycle_duration_seconds=lifecycle_duration,
                attempt_duration_seconds=attempt_duration,
                ready_job=ready_job,
                **metrics,
                evidence=attempt_evidence,
            )
        )
    return profiles


def build_profile(
    repo: str,
    run_id: int,
    run_attempt: int | None = None,
    *,
    ready_job: str | None = None,
) -> RunProfile:
    """Compatibility wrapper returning one attempt (latest unless selected)."""

    selected = [run_attempt] if run_attempt is not None else None
    profiles = build_profiles(repo, run_id, attempts=selected, ready_job=ready_job)
    return profiles[-1]


def all_steps(profiles: list[RunProfile]) -> list[StepProfile]:
    steps: list[StepProfile] = []
    for profile in profiles:
        for job in profile.jobs:
            steps.extend(job.steps)
    return steps


def filtered_profile(profile: RunProfile, excluded_jobs: set[str]) -> RunProfile:
    if not excluded_jobs:
        return profile
    jobs = [job for job in profile.jobs if job.name not in excluded_jobs]
    metrics = _aggregate_jobs(jobs, profile.ready_job, profile.created_at)
    evidence = {**profile.evidence, "excluded_jobs": sorted(excluded_jobs)}
    return replace(profile, jobs=jobs, evidence=evidence, **metrics)


def print_text(profiles: list[RunProfile], *, top: int) -> None:
    for profile in profiles:
        short_sha = (profile.head_sha or "")[:10] or "unknown"
        conclusion = profile.conclusion or "-"
        workflow = profile.workflow_name or "-"
        event = profile.event or "-"
        print(
            f"GitHub Actions wall-time profile: run {profile.run_id} "
            f"attempt {profile.run_attempt} ({workflow}/{event}, {short_sha}, "
            f"{profile.status}/{conclusion}, attempt {format_duration(profile.attempt_duration_seconds)}, "
            f"lifecycle {format_duration(profile.lifecycle_duration_seconds)})"
        )
        if profile.html_url:
            print(f"  {profile.html_url}")
        print(
            f"  observed job span {format_duration(profile.job_span_seconds)}, "
            f"job occupancy {format_duration(profile.job_occupancy_seconds)}, "
            f"cancelled occupancy {format_duration(profile.cancelled_job_occupancy_seconds)}, "
            f"failed occupancy {format_duration(profile.failed_job_occupancy_seconds)}"
        )
        if profile.ready_job:
            print(
                f"  ready milestone {profile.ready_job}: "
                f"{profile.ready_at or '-'} ({profile.ready_conclusion or '-'})"
            )
        for job in sorted(profile.jobs, key=lambda item: item.duration_seconds or -1, reverse=True):
            conclusion = job.conclusion or "-"
            print(
                f"  {format_duration(job.duration_seconds):>8}  {job.name} "
                f"(queue {format_duration(job.queue_duration_seconds)}, "
                f"{job.status}/{conclusion}, runner {job.runner_name or '-'})"
            )
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
                f"run {step.run_id} attempt {step.run_attempt} / {step.job_name} / {step.name} "
                f"({step.status}/{conclusion})"
            )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    args = parse_args()
    try:
        profiles = [
            profile
            for run_id in args.run_id
            for profile in build_profiles(
                args.repo,
                run_id,
                attempts=args.attempt,
                ready_job=args.ready_job,
            )
        ]
    except (RuntimeError, ValueError) as exc:
        print(f"Failed to fetch CI profile: {exc}", file=sys.stderr)
        return 2

    excluded_jobs = set(args.exclude_job or [])
    profiles = [filtered_profile(profile, excluded_jobs) for profile in profiles]
    if args.json:
        json.dump([asdict(profile) for profile in profiles], sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print_text(profiles, top=args.top)
    return 0


if __name__ == "__main__":
    os.chdir(repo_root())
    sys.exit(main())
