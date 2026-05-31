#!/usr/bin/env python3
"""Wait for exact-SHA push workflows, then verify live deploy state."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path


EXIT_SUCCESS = 0
EXIT_WORKFLOW_FAILURE = 10
EXIT_TIMEOUT = 11
EXIT_NO_RUNS = 12
EXIT_LIVE_DRIFT = 13

ACCEPTED_CONCLUSIONS = {"success", "neutral", "skipped"}
RUNTIME_HEALTH = {"healthy"}
DEPLOY_AND_VERIFY = "Deploy and Verify"
CI_WORKFLOW = "CI"
DEPLOY_AND_VERIFY_JOB = "Deploy demo + canary + hosted live QA"
DEPLOY_GATE_JOB = "Queue deploy behind earlier main SHAs + green CI"
RUNTIME_IMAGE_WORKFLOW = "Publish Runtime Image"
RUNTIME_IMAGE_JOB = "build-and-push"
CANARY_SURFACE = "Canary"
DEFAULT_CANARY_CONTAINER_NAME = "longhouse-" + os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN", "demo")
DEFAULT_CANARY_HEALTH_URL = "https://" + os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN", "demo") + ".longhouse.ai/api/health"


class NoRunsError(RuntimeError):
    pass


class PollTimeoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunInfo:
    databaseId: int
    workflowName: str
    status: str
    conclusion: str | None
    url: str
    headSha: str | None = None
    createdAt: str | None = None
    event: str | None = None


@dataclass(frozen=True)
class SurfaceInfo:
    sha: str
    health: str
    raw: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wait for push-triggered GitHub Actions runs for one exact SHA, then verify live deploy state."
    )
    parser.add_argument("--sha", help="Commit SHA to monitor. Defaults to HEAD.")
    parser.add_argument("--repo", default="cipher982/longhouse", help="GitHub repo in OWNER/REPO form.")
    parser.add_argument("--timeout", type=int, default=3600, help="Overall timeout in seconds. Default: 3600.")
    parser.add_argument(
        "--initial-timeout",
        type=int,
        default=180,
        help="How long to wait for the first push workflow run to appear. Default: 180.",
    )
    parser.add_argument("--poll", type=int, default=10, help="Polling interval in seconds. Default: 10.")
    parser.add_argument(
        "--skip-live",
        action="store_true",
        help="Skip live deploy verification. Useful for replaying old SHAs.",
    )
    parser.add_argument(
        "--heartbeat",
        type=int,
        default=60,
        help="Emit a waiting heartbeat every N seconds while workflows are still running. Default: 60.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON result object.")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, env=merged_env)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "command failed")
    return proc


def resolve_head_sha(root: Path) -> str:
    proc = run(["git", "rev-parse", "HEAD"], cwd=root)
    return proc.stdout.strip()


def resolve_commit_sha(root: Path, rev: str) -> str:
    proc = run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=root)
    return proc.stdout.strip()


def fetch_runs_for_event(repo: str, sha: str, event: str) -> list[RunInfo]:
    proc = run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--commit",
            sha,
            "--event",
            event,
            "--limit",
            "100",
            "--json",
            "databaseId,workflowName,status,conclusion,url,headSha,createdAt,event",
        ]
    )
    payload = json.loads(proc.stdout or "[]")
    runs: list[RunInfo] = []
    for item in payload:
        if item.get("headSha") != sha:
            continue
        runs.append(
            RunInfo(
                databaseId=int(item["databaseId"]),
                workflowName=item.get("workflowName") or f"run-{item['databaseId']}",
                status=item.get("status") or "",
                conclusion=item.get("conclusion"),
                url=item.get("url") or "",
                headSha=item.get("headSha"),
                createdAt=item.get("createdAt"),
                event=item.get("event"),
            )
        )
    return runs


def fetch_runs(repo: str, sha: str) -> list[RunInfo]:
    runs_by_id: dict[int, RunInfo] = {}
    for event in ("push", "workflow_dispatch"):
        for run_info in fetch_runs_for_event(repo, sha, event):
            runs_by_id[run_info.databaseId] = run_info
    runs = list(runs_by_id.values())
    runs.sort(key=lambda run: (run.workflowName, run.databaseId))
    return runs


def fetch_recent_push_runs(repo: str, limit: int = 12) -> list[RunInfo]:
    proc = run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--event",
            "push",
            "--limit",
            str(limit),
            "--json",
            "databaseId,workflowName,status,conclusion,url,headSha,createdAt",
        ]
    )
    payload = json.loads(proc.stdout or "[]")
    runs: list[RunInfo] = []
    for item in payload:
        head_sha = item.get("headSha") or ""
        runs.append(
            RunInfo(
                databaseId=int(item["databaseId"]),
                workflowName=item.get("workflowName") or f"run-{item['databaseId']}",
                status=item.get("status") or "",
                conclusion=item.get("conclusion"),
                url=item.get("url") or "",
                headSha=head_sha,
                createdAt=item.get("createdAt"),
            )
        )
    return runs


def fetch_run_jobs(repo: str, run_id: int) -> list[dict]:
    proc = run(
        [
            "gh",
            "run",
            "view",
            str(run_id),
            "-R",
            repo,
            "--json",
            "jobs",
        ]
    )
    payload = json.loads(proc.stdout or "{}")
    return payload.get("jobs") or []


def fetch_first_parent_sha(repo: str, sha: str) -> str | None:
    proc = run(
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}",
            "--jq",
            ".parents[0].sha // empty",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def print_ci_profile(root: Path, repo: str, runs: list[RunInfo]) -> None:
    """Print best-effort deterministic job/step timings for completed runs."""
    if not runs:
        return
    profiler = root / "scripts" / "ops" / "ci-profile.py"
    if not profiler.exists():
        return
    cmd = [str(profiler), "--repo", repo, "--top", "12"]
    for run_info in runs:
        cmd.extend(["--run-id", str(run_info.databaseId)])
    proc = run(cmd, cwd=root, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if detail:
            print(f"CI profile unavailable: {detail}", file=sys.stderr)
        return
    if proc.stdout.strip():
        print("", file=sys.stderr)
        print(proc.stdout.rstrip(), file=sys.stderr)


def fetch_remote_head(repo: str, branch: str = "main") -> str | None:
    proc = run(
        [
            "gh",
            "api",
            f"repos/{repo}/git/ref/heads/{branch}",
            "--jq",
            ".object.sha",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def fingerprint(runs: list[RunInfo]) -> tuple[tuple[int, str, str | None], ...]:
    return tuple((run.databaseId, run.status, run.conclusion) for run in runs)


def runs_succeeded(runs: list[RunInfo]) -> bool:
    return all(run.status == "completed" and (run.conclusion in ACCEPTED_CONCLUSIONS) for run in runs)


def failed_runs(runs: list[RunInfo]) -> list[RunInfo]:
    return [
        run
        for run in runs
        if run.status == "completed" and run.conclusion not in ACCEPTED_CONCLUSIONS
    ]


def select_load_bearing_runs(runs: list[RunInfo]) -> tuple[list[RunInfo], list[str]]:
    workflow_names = {run.workflowName for run in runs}
    required_names: list[str] = []

    if DEPLOY_AND_VERIFY in workflow_names:
        required_names.append(DEPLOY_AND_VERIFY)

    if not required_names:
        return runs, []

    selected: list[RunInfo] = []
    for workflow_name in required_names:
        matches = [run for run in runs if run.workflowName == workflow_name]
        matches.sort(key=lambda run: run.databaseId, reverse=True)
        selected.extend(matches[:1])
    selected.sort(key=lambda run: (run.workflowName, run.databaseId))
    return selected, required_names


def summarize_runs(runs: list[RunInfo], short_sha: str, scope_label: str | None = None) -> str:
    if scope_label:
        lines = [f"Watching {scope_label} for {short_sha}:"]
    else:
        lines = [f"Watching push workflows for {short_sha}:"]
    for run in runs:
        conclusion = run.conclusion or "-"
        event = f" [{run.event}]" if run.event and run.event != "push" else ""
        lines.append(f"  - {run.workflowName} #{run.databaseId}{event}: {run.status}/{conclusion}")
    return "\n".join(lines)


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"


def parse_github_timestamp(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def current_duration_suffix(started_at: str | None) -> str:
    started = parse_github_timestamp(started_at)
    if started is None:
        return ""
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if elapsed < 1:
        return ""
    return f", {format_elapsed(elapsed)}"


def field(item: dict, snake_name: str, camel_name: str | None = None) -> str | None:
    value = item.get(snake_name)
    if value is None and camel_name:
        value = item.get(camel_name)
    if value is None:
        return None
    return str(value)


def active_step(job: dict) -> dict | None:
    steps = job.get("steps") or []
    for step in steps:
        if field(step, "status") == "in_progress":
            return step
    for step in steps:
        if field(step, "status") not in {None, "completed"}:
            return step
    return None


def active_job(jobs: list[dict]) -> dict | None:
    for job in jobs:
        if field(job, "status") == "in_progress":
            return job
    for job in jobs:
        if field(job, "status") not in {None, "completed"}:
            return job
    return None


def describe_run_progress(repo: str, run_info: RunInfo) -> str:
    try:
        jobs = fetch_run_jobs(repo, run_info.databaseId)
    except RuntimeError:
        return f"{run_info.workflowName} #{run_info.databaseId}: {run_info.status}/{run_info.conclusion or '-'}"

    job = active_job(jobs)
    if job is None:
        return f"{run_info.workflowName} #{run_info.databaseId}: {run_info.status}/{run_info.conclusion or '-'}"

    job_name = field(job, "name") or f"job-{field(job, 'databaseId', 'databaseId') or '?'}"
    step = active_step(job)
    if step is None:
        status = field(job, "status") or "unknown"
        return f"{run_info.workflowName} #{run_info.databaseId} / {job_name}: {status}"

    step_name = field(step, "name") or "current step"
    status = field(step, "status") or "unknown"
    started_at = field(step, "started_at", "startedAt")
    return (
        f"{run_info.workflowName} #{run_info.databaseId} / {job_name} / "
        f"{step_name}: {status}{current_duration_suffix(started_at)}"
    )


def find_run(runs: list[RunInfo], workflow_name: str) -> RunInfo | None:
    matches = [run_info for run_info in runs if run_info.workflowName == workflow_name]
    if not matches:
        return None
    matches.sort(key=lambda run_info: run_info.databaseId, reverse=True)
    return matches[0]


def describe_blocking_workflow(repo: str, workflow_name: str, sha: str, runs: list[RunInfo] | None = None) -> str:
    candidate_runs = runs if runs is not None else fetch_runs(repo, sha)
    run_info = find_run(candidate_runs, workflow_name)
    if run_info is None:
        return f"{workflow_name}: no exact-SHA run found for {sha[:10]}"
    if run_info.status == "completed":
        return f"{workflow_name} #{run_info.databaseId}: completed/{run_info.conclusion or '-'}"
    return describe_run_progress(repo, run_info)


def describe_deploy_run_blocker(repo: str, sha: str, runs: list[RunInfo], deploy_run: RunInfo) -> str | None:
    try:
        jobs = fetch_run_jobs(repo, deploy_run.databaseId)
    except RuntimeError:
        return None

    gate_job = next((job for job in jobs if field(job, "name") == DEPLOY_GATE_JOB), None)
    if gate_job and field(gate_job, "status") == "in_progress":
        step = active_step(gate_job)
        if step is None:
            return f"{DEPLOY_AND_VERIFY} #{deploy_run.databaseId} / {DEPLOY_GATE_JOB}: in_progress"

        step_name = field(step, "name") or "current gate step"
        status = field(step, "status") or "unknown"
        started_at = field(step, "started_at", "startedAt")

        if step_name == "Wait for full CI gate":
            blocker = describe_blocking_workflow(repo, CI_WORKFLOW, sha, runs)
            return f"{DEPLOY_AND_VERIFY} #{deploy_run.databaseId} / gate -> {blocker}"

        if step_name == "Wait for runtime image publish":
            blocker = describe_blocking_workflow(repo, RUNTIME_IMAGE_WORKFLOW, sha, runs)
            return f"{DEPLOY_AND_VERIFY} #{deploy_run.databaseId} / gate -> {blocker}"

        if step_name == "Wait for previous main deploy to clear":
            parent_sha = fetch_first_parent_sha(repo, sha)
            if parent_sha:
                blocker = describe_blocking_workflow(repo, DEPLOY_AND_VERIFY, parent_sha)
                return f"{DEPLOY_AND_VERIFY} #{deploy_run.databaseId} / gate -> previous main {blocker}"

        return (
            f"{DEPLOY_AND_VERIFY} #{deploy_run.databaseId} / {DEPLOY_GATE_JOB} / "
            f"{step_name}: {status}{current_duration_suffix(started_at)}"
        )

    active = active_job(jobs)
    if active is None:
        return None
    job_name = field(active, "name") or "active job"
    step = active_step(active)
    if step is None:
        return f"{DEPLOY_AND_VERIFY} #{deploy_run.databaseId} / {job_name}: {field(active, 'status') or 'unknown'}"
    step_name = field(step, "name") or "current step"
    status = field(step, "status") or "unknown"
    started_at = field(step, "started_at", "startedAt")
    return (
        f"{DEPLOY_AND_VERIFY} #{deploy_run.databaseId} / {job_name} / "
        f"{step_name}: {status}{current_duration_suffix(started_at)}"
    )


def describe_ship_blocker(repo: str, sha: str, runs: list[RunInfo]) -> str | None:
    for run_info in runs:
        if run_info.status != "completed" and run_info.workflowName == DEPLOY_AND_VERIFY:
            blocker = describe_deploy_run_blocker(repo, sha, runs, run_info)
            if blocker:
                return blocker

    for run_info in runs:
        if run_info.status == "completed":
            continue
        return describe_run_progress(repo, run_info)
    return None


def summarize_incomplete_runs(repo: str, sha: str, runs: list[RunInfo]) -> str:
    blocker = describe_ship_blocker(repo, sha, runs)
    if blocker:
        return blocker

    parts: list[str] = []
    for run in runs:
        if run.status == "completed":
            continue
        conclusion = run.conclusion or "-"
        parts.append(f"{run.workflowName} #{run.databaseId}: {run.status}/{conclusion}")
    return ", ".join(parts) or "waiting on GitHub Actions"


def summarize_recent_runs(runs: list[RunInfo]) -> list[dict[str, str | int | None]]:
    summary: list[dict[str, str | int | None]] = []
    seen: set[tuple[str, str]] = set()
    for run in runs:
        head_sha = run.headSha or ""
        short_sha = head_sha[:10] if head_sha else "unknown"
        key = (short_sha, run.workflowName)
        if key in seen:
            continue
        seen.add(key)
        summary.append(
            {
                "head_sha": head_sha,
                "short_sha": short_sha,
                "workflow_name": run.workflowName,
                "run_id": run.databaseId,
                "status": run.status,
                "conclusion": run.conclusion,
                "url": run.url,
            }
        )
        if len(summary) >= 8:
            break
    return summary


def wait_for_workflows(args: argparse.Namespace, sha: str) -> list[RunInfo]:
    start = time.time()
    initial_deadline = start + args.initial_timeout
    deadline = start + args.timeout
    last_seen: tuple[tuple[int, str, str | None], ...] | None = None
    last_scope_label: str | None = None
    next_heartbeat = start + max(args.heartbeat, 1) if args.heartbeat > 0 else None

    while True:
        now = time.time()
        try:
            runs = fetch_runs(args.repo, sha)
        except RuntimeError as exc:
            if now >= deadline:
                raise PollTimeoutError(f"Timed out while polling GitHub Actions: {exc}") from exc
            print(f"GitHub Actions poll failed: {exc}. Retrying in {args.poll}s...", file=sys.stderr)
            time.sleep(args.poll)
            continue

        if not runs:
            if now >= initial_deadline:
                raise NoRunsError("No push-triggered workflow runs appeared for the target SHA.")
            print(f"Waiting for push workflows for {sha[:10]} to appear...", file=sys.stderr)
            time.sleep(args.poll)
            continue

        monitored_runs, required_names = select_load_bearing_runs(runs)
        scope_label = ", ".join(required_names) if required_names else None
        current = fingerprint(monitored_runs)

        if scope_label != last_scope_label:
            if scope_label:
                ignored = sorted({run.workflowName for run in runs if run.workflowName not in required_names})
                if ignored:
                    print(
                        f"Load-bearing ship scope for {sha[:10]}: {scope_label} "
                        f"(ignoring: {', '.join(ignored)})",
                        file=sys.stderr,
                    )
                else:
                    print(f"Load-bearing ship scope for {sha[:10]}: {scope_label}", file=sys.stderr)
            else:
                print(f"Ship scope for {sha[:10]}: all push workflows", file=sys.stderr)
            last_scope_label = scope_label

        if current != last_seen:
            print(summarize_runs(monitored_runs, sha[:10], scope_label), file=sys.stderr)
            last_seen = current
            if args.heartbeat > 0:
                next_heartbeat = now + max(args.heartbeat, 1)

        if all(run.status == "completed" for run in monitored_runs):
            return runs

        if next_heartbeat is not None and now >= next_heartbeat:
            print(
                f"Still waiting on {sha[:10]} after {format_elapsed(now - start)}: "
                f"{summarize_incomplete_runs(args.repo, sha, runs)}",
                file=sys.stderr,
            )
            next_heartbeat = now + max(args.heartbeat, 1)

        if now >= deadline:
            raise PollTimeoutError(f"Timed out waiting for push workflows for {sha[:10]}")

        time.sleep(args.poll)


def parse_deploy_status(output: str) -> dict[str, SurfaceInfo]:
    surfaces: dict[str, SurfaceInfo] = {}
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Surface") or stripped.startswith("-------") or stripped.startswith("⚠"):
            continue
        parts = re.split(r"\s{2,}", stripped)
        if len(parts) < 2:
            continue
        surface = parts[0]
        if surface == "Local HEAD":
            continue
        if len(parts) < 3:
            continue
        surfaces[surface] = SurfaceInfo(sha=parts[1], health=parts[2], raw=stripped)
    return surfaces


def verify_live_state(root: Path, repo: str, sha: str, runs: list[RunInfo]) -> tuple[dict[str, SurfaceInfo], list[str], str]:
    proc = run(
        [str(root / "scripts" / "ops" / "deploy-status.sh")],
        cwd=root,
        env={
            "CANARY_CONTAINER_NAME": os.environ.get("CANARY_CONTAINER_NAME") or DEFAULT_CANARY_CONTAINER_NAME,
            "CANARY_HEALTH_URL": os.environ.get("CANARY_HEALTH_URL") or DEFAULT_CANARY_HEALTH_URL,
        },
    )
    raw = proc.stdout
    surfaces = parse_deploy_status(raw)
    short_sha = sha[:10]
    errors: list[str] = []
    jobs_by_run_id: dict[int, list[dict]] = {}

    def job_succeeded(run: RunInfo, expected_job_name: str) -> bool:
        if run.status != "completed" or run.conclusion != "success":
            return False
        jobs = jobs_by_run_id.get(run.databaseId)
        if jobs is None:
            jobs = fetch_run_jobs(repo, run.databaseId)
            jobs_by_run_id[run.databaseId] = jobs
        for job in jobs:
            if job.get("name") == expected_job_name and job.get("conclusion") == "success":
                return True
        return False

    runtime_image_published = any(
        run.workflowName == RUNTIME_IMAGE_WORKFLOW and job_succeeded(run, RUNTIME_IMAGE_JOB)
        for run in runs
    )
    if not runtime_image_published:
        raw = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("⚠")
        ).rstrip() + "\n"

    def require_surface(surface_name: str, allowed_health: set[str], *, check_sha: bool) -> None:
        surface = surfaces.get(surface_name)
        if surface is None:
            errors.append(f"Missing {surface_name!r} in deploy-status output")
            return
        if check_sha and surface.sha != short_sha:
            errors.append(f"{surface_name} is on {surface.sha}, expected {short_sha}")
        if surface.health not in allowed_health:
            errors.append(f"{surface_name} health is {surface.health}, expected one of {sorted(allowed_health)}")

    if any(
        run.workflowName == DEPLOY_AND_VERIFY and job_succeeded(run, DEPLOY_AND_VERIFY_JOB)
        for run in runs
    ):
        require_surface("Demo runtime", RUNTIME_HEALTH, check_sha=runtime_image_published)
        require_surface(CANARY_SURFACE, RUNTIME_HEALTH, check_sha=runtime_image_published)

    return surfaces, errors, raw


def emit_json(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> int:
    args = parse_args()
    root = repo_root()
    target_sha = resolve_commit_sha(root, args.sha.strip()) if args.sha else resolve_head_sha(root)
    short_sha = target_sha[:10]

    try:
        runs = wait_for_workflows(args, target_sha)
    except NoRunsError as exc:
        message = str(exc)
        remote_head = fetch_remote_head(args.repo)
        recent_runs = summarize_recent_runs(fetch_recent_push_runs(args.repo))
        payload = {
            "repo": args.repo,
            "target_sha": target_sha,
            "result": "no_runs",
            "message": message,
            "workflows": [],
            "remote_head_sha": remote_head,
            "recent_push_runs": recent_runs,
        }
        if args.json:
            emit_json(payload)
        else:
            print(f"{message} ({args.initial_timeout}s).", file=sys.stderr)
            if remote_head:
                print(f"Current remote main head: {remote_head[:10]}", file=sys.stderr)
            if recent_runs:
                print("Recent push workflow attribution:", file=sys.stderr)
                for item in recent_runs:
                    conclusion = item["conclusion"] or "-"
                    print(
                        f"  - {item['short_sha']} {item['workflow_name']} #{item['run_id']}: "
                        f"{item['status']}/{conclusion}",
                        file=sys.stderr,
                    )
            print(
                "No exact-SHA workflows were found. Do not infer success from another SHA unless "
                "a later descendant-coverage mode explicitly supports that.",
                file=sys.stderr,
            )
        return EXIT_NO_RUNS
    except PollTimeoutError as exc:
        payload = {
            "repo": args.repo,
            "target_sha": target_sha,
            "result": "timeout",
            "message": str(exc),
            "workflows": [],
        }
        if args.json:
            emit_json(payload)
        else:
            print(str(exc), file=sys.stderr)
        return EXIT_TIMEOUT

    monitored_runs, required_names = select_load_bearing_runs(runs)
    workflow_payload = [asdict(run) for run in runs]
    monitored_payload = [asdict(run) for run in monitored_runs]

    if not runs_succeeded(monitored_runs):
        failures = failed_runs(monitored_runs)
        payload = {
            "repo": args.repo,
            "target_sha": target_sha,
            "result": "workflow_failure",
            "workflows": workflow_payload,
            "monitored_workflows": monitored_payload,
            "required_workflow_names": required_names,
            "failed_workflows": [asdict(run) for run in failures],
        }
        if args.json:
            emit_json(payload)
        else:
            if required_names:
                print(
                    f"Load-bearing workflows failed for {short_sha}: {', '.join(required_names)}",
                    file=sys.stderr,
                )
            else:
                print(f"Push workflows failed for {short_sha}:", file=sys.stderr)
            for run in failures:
                conclusion = run.conclusion or "unknown"
                print(f"  - {run.workflowName} #{run.databaseId}: {conclusion}", file=sys.stderr)
                print(f"    {run.url}", file=sys.stderr)
                print(f"    Inspect logs: gh run view {run.databaseId} --log-failed", file=sys.stderr)
            print_ci_profile(root, args.repo, monitored_runs)
        return EXIT_WORKFLOW_FAILURE

    live_surfaces: dict[str, dict] | None = None
    live_errors: list[str] = []
    live_output = ""
    if not args.skip_live:
        surfaces, live_errors, live_output = verify_live_state(root, args.repo, target_sha, runs)
        live_surfaces = {name: asdict(info) for name, info in surfaces.items()}
        if live_errors:
            payload = {
                "repo": args.repo,
                "target_sha": target_sha,
                "result": "live_drift",
                "workflows": workflow_payload,
                "live": live_surfaces,
                "live_errors": live_errors,
            }
            if args.json:
                emit_json(payload)
            else:
                print(f"Live deploy verification failed for {short_sha}:", file=sys.stderr)
                for error in live_errors:
                    print(f"  - {error}", file=sys.stderr)
                if live_output:
                    print("", file=sys.stderr)
                    print(live_output.rstrip(), file=sys.stderr)
            return EXIT_LIVE_DRIFT

    payload = {
        "repo": args.repo,
        "target_sha": target_sha,
        "result": "success",
        "workflows": workflow_payload,
        "monitored_workflows": monitored_payload,
        "required_workflow_names": required_names,
    }
    if live_surfaces is not None:
        payload["live"] = live_surfaces

    if args.json:
        emit_json(payload)
    else:
        print(f"Ship verification passed for {short_sha}.", file=sys.stderr)
        print_ci_profile(root, args.repo, monitored_runs)
        if live_output:
            print("", file=sys.stderr)
            print(live_output.rstrip(), file=sys.stderr)
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
