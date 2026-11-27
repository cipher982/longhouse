#!/usr/bin/env python3
"""Wait for exact-SHA push workflows, then verify fast live deploy state."""

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
RUNTIME_HEALTH = {"healthy", "degraded"}
CI_WORKFLOW = "CI"
RELEASE_WORKFLOW = "Release"
RELEASE_WORKFLOW_FILE = "release.yml"
RELEASE_CANARY_JOB = "Canary deploy and smoke"
DEFAULT_CANARY_SUBDOMAIN = os.environ.get("HOSTED_CANARY_SUBDOMAIN", "release-canary-a")
CANARY_SURFACE = f"Canary {DEFAULT_CANARY_SUBDOMAIN}"
DEFAULT_CANARY_HEALTH_URL = f"https://{DEFAULT_CANARY_SUBDOMAIN}.longhouse.ai/api/health"
_FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")


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
    headBranch: str | None = None


@dataclass(frozen=True)
class SurfaceInfo:
    sha: str
    health: str
    build_identity: str = "-"
    raw: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wait for push-triggered GitHub Actions runs for one exact SHA, then verify fast live deploy state."
    )
    parser.add_argument("--sha", help="Commit SHA to monitor. Defaults to HEAD.")
    parser.add_argument("--repo", default="cipher982/longhouse", help="GitHub repo in OWNER/REPO form.")
    parser.add_argument("--timeout", type=int, default=3600, help="Overall timeout in seconds. Default: 3600.")
    parser.add_argument(
        # GitHub queues the push event; it does not always deliver it promptly.
        # On 2026-08-26 a push at 16:40 produced its first run at 16:48, and a
        # release the same morning fired nineteen minutes late. Three minutes
        # declared those pushes dead and sent the operator off to dispatch the
        # workflows by hand -- which is how that release ended up with two
        # publish runs racing each other. Waiting costs nothing here: once a run
        # appears, --timeout governs. Only a genuinely undelivered event should
        # reach this deadline.
        "--initial-timeout",
        type=int,
        default=1800,
        help="How long to wait for the first push workflow run to appear. Default: 1800.",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=45,
        help=f"Polling interval in seconds. Default: 45; values under {MIN_POLL_SECONDS} are raised to it (GitHub rate budget).",
    )
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


MIN_POLL_SECONDS = 30
# etag + body per REST path. A 304 answer to If-None-Match is not charged against the
# GitHub rate limit, so an unchanged run list costs nothing while a ship waits.
_ETAG_CACHE: dict[str, tuple[str, object]] = {}


def gh_api_cached(path: str):
    """GET a GitHub REST path through gh with ETag caching; returns the parsed JSON body."""

    cached = _ETAG_CACHE.get(path)
    cmd = ["gh", "api", "-i"]
    if cached:
        cmd += ["-H", f"If-None-Match: {cached[0]}"]
    cmd.append(path)
    proc = run(cmd, check=False)
    out = (proc.stdout or "").replace("\r\n", "\n")
    head, _, body = out.partition("\n\n")
    status_line = head.splitlines()[0] if head.strip() else ""
    if " 304" in status_line and cached:
        return cached[1]
    if proc.returncode != 0 or " 200" not in status_line:
        raise RuntimeError((proc.stderr or status_line or "gh api failed").strip())
    payload = json.loads(body or "{}")
    etag = next((ln.split(":", 1)[1].strip() for ln in head.splitlines() if ln.lower().startswith("etag:")), "")
    if etag:
        _ETAG_CACHE[path] = (etag, payload)
    return payload


def fetch_runs_for_sha(repo: str, sha: str) -> list[RunInfo]:
    """Every push or dispatch run of one commit: one conditional call instead of one list per event."""

    data = gh_api_cached(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")
    runs: list[RunInfo] = []
    for item in data.get("workflow_runs") or []:
        if item.get("head_sha") != sha or item.get("event") not in ("push", "workflow_dispatch"):
            continue
        if is_release_run(item):
            continue  # read below, by title
        runs.append(run_from_api(item))
    # Release starts from CI completing (workflow_run), so its own head_sha can be main's head rather than
    # this commit; its title is `Release <sha>`.
    released = gh_api_cached(f"repos/{repo}/actions/workflows/{RELEASE_WORKFLOW_FILE}/runs?branch=main&per_page=30")
    for item in released.get("workflow_runs") or []:
        if is_release_run(item) and release_sha(item) == sha:
            runs.append(run_from_api(item))
    return runs


def is_release_run(item: dict) -> bool:
    """A Release run. Its API ``name`` is its run-name, ``Release <sha>`` (since 2026-11-22), not the
    workflow's name, so match the workflow file instead; a bare ``Release`` covers older runs."""
    path = str(item.get("path") or "")
    return path.endswith(f"/{RELEASE_WORKFLOW_FILE}") or item.get("name") == RELEASE_WORKFLOW


def release_sha(item: dict) -> str:
    """The commit a Release run is about: the sha in its title, else its head_sha."""
    found = _FULL_SHA.search(str(item.get("display_title") or item.get("name") or ""))
    return found.group(0) if found else str(item.get("head_sha") or "")


def run_from_api(item: dict) -> RunInfo:
    return RunInfo(
        databaseId=int(item["id"]),
        # Every Release run reads as the workflow, whatever its run-name says.
        workflowName=RELEASE_WORKFLOW if is_release_run(item) else (item.get("name") or f"run-{item['id']}"),
        status=item.get("status") or "",
        conclusion=item.get("conclusion"),
        url=item.get("html_url") or "",
        headSha=item.get("head_sha"),
        createdAt=item.get("created_at"),
        event=item.get("event"),
        headBranch=item.get("head_branch"),
    )


def default_branch() -> str | None:
    """The branch a ship targets, from the checkout's own remote head.

    No network call: `origin/HEAD` is set by clone and by every fetch, and the
    monitor already runs in a checkout of the repository it is shipping.
    """

    try:
        proc = run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            check=False,
        )
    except OSError:
        return None
    value = (proc.stdout or "").strip()
    return value.split("/", 1)[1] if proc.returncode == 0 and "/" in value else None


def fetch_runs(repo: str, sha: str) -> list[RunInfo]:
    runs = fetch_runs_for_sha(repo, sha)
    # The same commit often exists on a topic branch and on the default branch:
    # a ship pushes a branch, then main. A failure on the topic branch is not a
    # fact about the ship -- a branch run of the *same* SHA failed on a git fetch
    # network timeout while main's own run was green, and the monitor read the
    # branch run as the ship's verdict. Prefer the default branch's runs whenever
    # that branch has any for this SHA; otherwise keep everything (dispatch-only
    # shas, or a repository whose remote head is not resolvable here).
    branch = default_branch()
    if branch is not None:
        on_default = [run_info for run_info in runs if run_info.headBranch == branch]
        if on_default:
            runs = on_default
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
    return [run for run in runs if run.status == "completed" and run.conclusion not in ACCEPTED_CONCLUSIONS]


def job_succeeded(repo: str, run: RunInfo, expected_job_name: str) -> bool:
    if run.status != "completed" or run.conclusion != "success":
        return False
    return any(job.get("name") == expected_job_name and job.get("conclusion") == "success" for job in fetch_run_jobs(repo, run.databaseId))


def select_load_bearing_runs(runs: list[RunInfo]) -> tuple[list[RunInfo], list[str]]:
    """CI and Release speak for a ship; other push workflows (Terminal Truth, Launch Gate) do not hold it."""
    workflow_names = {run.workflowName for run in runs}
    required_names = [name for name in (CI_WORKFLOW, RELEASE_WORKFLOW) if name in workflow_names]
    if not required_names:
        return runs, []

    selected: list[RunInfo] = []
    for workflow_name in required_names:
        matches = [run for run in runs if run.workflowName == workflow_name]
        matches.sort(key=lambda run: run.databaseId, reverse=True)
        selected.extend(matches[:1])
    selected.sort(key=lambda run: (run.workflowName, run.databaseId))
    return selected, required_names


def awaiting_release(runs: list[RunInfo]) -> bool:
    """CI has passed for this commit but its Release run has not started: Release starts when CI completes."""
    ci = [run for run in runs if run.workflowName == CI_WORKFLOW]
    if not ci or any(run.workflowName == RELEASE_WORKFLOW for run in runs):
        return False
    newest = max(ci, key=lambda run: run.databaseId)
    return newest.status == "completed" and newest.conclusion == "success"


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
    return f"{run_info.workflowName} #{run_info.databaseId} / {job_name} / {step_name}: {status}{current_duration_suffix(started_at)}"


def describe_ship_blocker(repo: str, sha: str, runs: list[RunInfo]) -> str | None:
    for run_info in runs:
        if run_info.status != "completed":
            return describe_run_progress(repo, run_info)
    if awaiting_release(runs):
        return f"{CI_WORKFLOW} passed; waiting for the {RELEASE_WORKFLOW} run for {sha[:10]} to start"
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
                        f"Load-bearing ship scope for {sha[:10]}: {scope_label} (ignoring: {', '.join(ignored)})",
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

        if all(run.status == "completed" for run in monitored_runs) and not awaiting_release(runs):
            return runs

        if next_heartbeat is not None and now >= next_heartbeat:
            print(
                f"Still waiting on {sha[:10]} after {format_elapsed(now - start)}: {summarize_incomplete_runs(args.repo, sha, runs)}",
                file=sys.stderr,
            )
            next_heartbeat = now + max(args.heartbeat, 1)

        if now >= deadline:
            raise PollTimeoutError(f"Timed out waiting for push workflows for {sha[:10]}")

        time.sleep(args.poll)


def was_superseded(runs: list[RunInfo]) -> bool:
    """Main CI drops queued runs for commits main has moved past, and GitHub keeps one pending Release: a
    cancelled run means another commit carries this one; it is not a failure of this commit."""
    return any(run.status == "completed" and run.conclusion == "cancelled" for run in runs)


def contains_commit(root: Path, descendant: str, ancestor: str) -> bool:
    run(["git", "-C", str(root), "fetch", "--quiet", "origin", "main"], check=False)
    return run(["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant], check=False).returncode == 0


def wait_following_coverage(args: argparse.Namespace, root: Path, sha: str) -> tuple[str, list[RunInfo]]:
    """Wait on `sha`; when its runs were superseded, follow main's head, which contains it.

    This is the descendant-coverage mode: a newer main commit's Release deploys
    everything since what the tenant serves, so the ship result is that commit's result.
    """
    while True:
        runs = wait_for_workflows(args, sha)
        head = fetch_remote_head(args.repo)
        if not head or head == sha or not was_superseded(runs) or not contains_commit(root, head, sha):
            return sha, runs
        print(
            f"{sha[:10]} was superseded: its queued runs were dropped because main moved to {head[:10]}, "
            f"which contains it. Following {head[:10]}; its deploy ships this change.",
            file=sys.stderr,
        )
        sha = head


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
        if len(parts) < 3:
            continue
        surface = parts[0]
        if surface.startswith("Canary "):
            # deploy-status includes the hosted canary subdomain in the
            # display label; verification addresses the canonical surface.
            surface = CANARY_SURFACE
        if surface == "Local HEAD":
            continue
        build_identity = parts[3] if len(parts) >= 4 else "-"
        surfaces[surface] = SurfaceInfo(
            sha=parts[1],
            health=parts[2],
            build_identity=build_identity,
            raw=stripped,
        )
    return surfaces


def release_disposition(repo: str, runs: list[RunInfo]) -> str:
    """deployed (the Release canary job succeeded), no_runtime_change (Release had nothing to deploy) or unverified."""
    releases = [run for run in runs if run.workflowName == RELEASE_WORKFLOW and run.status == "completed" and run.conclusion == "success"]
    if not releases:
        return "unverified"
    if any(job_succeeded(repo, run, RELEASE_CANARY_JOB) for run in releases):
        return "deployed"
    return "no_runtime_change"


def verify_live_state(root: Path, repo: str, sha: str, runs: list[RunInfo]) -> tuple[dict[str, SurfaceInfo], list[str], str]:
    # Only a release that deployed must show: the canary serves this commit or one that contains it. Dogfood
    # follows the canary under its own gates and is printed in the status table for reference.
    deployed = release_disposition(repo, runs) == "deployed"

    def read_deploy_status() -> tuple[dict[str, SurfaceInfo], str]:
        proc = run(
            [str(root / "scripts" / "ops" / "deploy-status.sh")],
            cwd=root,
            env={
                "HOSTED_CANARY_HEALTH_URL": os.environ.get("HOSTED_CANARY_HEALTH_URL") or DEFAULT_CANARY_HEALTH_URL,
            },
        )
        raw = proc.stdout
        return parse_deploy_status(raw), raw

    def check_canary(surfaces: dict[str, SurfaceInfo]) -> list[str]:
        surface = surfaces.get(CANARY_SURFACE)
        if surface is None:
            return [f"Missing {CANARY_SURFACE!r} in deploy-status output"]
        errors: list[str] = []
        if surface.sha != sha[:10] and not contains_commit(root, surface.sha, sha):
            errors.append(f"{CANARY_SURFACE} is on {surface.sha}, expected {sha[:10]} or a commit that contains it")
        if surface.health not in RUNTIME_HEALTH:
            errors.append(f"{CANARY_SURFACE} health is {surface.health}, expected one of {sorted(RUNTIME_HEALTH)}")
        return errors

    def retryable(surfaces: dict[str, SurfaceInfo]) -> bool:
        surface = surfaces.get(CANARY_SURFACE)
        return surface is None or surface.sha == "-" or surface.health == "unreachable"

    surfaces: dict[str, SurfaceInfo] = {}
    raw = ""
    errors: list[str] = []
    for attempt in range(3):
        surfaces, raw = read_deploy_status()
        errors = check_canary(surfaces) if deployed else []
        if not errors or not retryable(surfaces) or attempt == 2:
            break
        time.sleep(2)

    return surfaces, errors, raw


def emit_json(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> int:
    args = parse_args()
    args.poll = max(args.poll, MIN_POLL_SECONDS)
    root = repo_root()
    target_sha = resolve_commit_sha(root, args.sha.strip()) if args.sha else resolve_head_sha(root)
    short_sha = target_sha[:10]

    requested_sha = target_sha
    try:
        target_sha, runs = wait_following_coverage(args, root, target_sha)
        short_sha = target_sha[:10]
        if target_sha != requested_sha:
            print(f"Verifying {short_sha} as the ship for {requested_sha[:10]} (descendant coverage).", file=sys.stderr)
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
                        f"  - {item['short_sha']} {item['workflow_name']} #{item['run_id']}: {item['status']}/{conclusion}",
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
    runtime_disposition = release_disposition(args.repo, runs) if runs_succeeded(monitored_runs) else "unverified"

    if not runs_succeeded(monitored_runs):
        failures = failed_runs(monitored_runs)
        payload = {
            "repo": args.repo,
            "target_sha": target_sha,
            "result": "workflow_failure",
            "runtime_disposition": runtime_disposition,
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
                "runtime_disposition": runtime_disposition,
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
        "runtime_disposition": runtime_disposition,
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
