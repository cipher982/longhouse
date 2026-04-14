#!/usr/bin/env python3
"""Wait for exact-SHA push workflows, then verify live deploy state."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path


EXIT_SUCCESS = 0
EXIT_WORKFLOW_FAILURE = 10
EXIT_TIMEOUT = 11
EXIT_NO_RUNS = 12
EXIT_LIVE_DRIFT = 13

ACCEPTED_CONCLUSIONS = {"success", "neutral", "skipped"}
CONTROL_PLANE_HEALTH = {"ok", "healthy"}
RUNTIME_HEALTH = {"healthy"}
DEPLOY_AND_VERIFY = "Deploy and Verify"
DEPLOY_CONTROL_PLANE = "Deploy Control Plane"


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
    createdAt: str | None = None


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
        default=90,
        help="How long to wait for the first push workflow run to appear. Default: 90.",
    )
    parser.add_argument("--poll", type=int, default=10, help="Polling interval in seconds. Default: 10.")
    parser.add_argument(
        "--skip-live",
        action="store_true",
        help="Skip live deploy verification. Useful for replaying old SHAs.",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON result object.")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "command failed")
    return proc


def resolve_head_sha(root: Path) -> str:
    proc = run(["git", "rev-parse", "HEAD"], cwd=root)
    return proc.stdout.strip()


def fetch_runs(repo: str, sha: str) -> list[RunInfo]:
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
            "push",
            "--limit",
            "100",
            "--json",
            "databaseId,workflowName,status,conclusion,url,headSha,createdAt",
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
                createdAt=item.get("createdAt"),
            )
        )
    runs.sort(key=lambda run: (run.workflowName, run.databaseId))
    return runs


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


def summarize_runs(runs: list[RunInfo], short_sha: str) -> str:
    lines = [f"Watching push workflows for {short_sha}:"]
    for run in runs:
        conclusion = run.conclusion or "-"
        lines.append(f"  - {run.workflowName} #{run.databaseId}: {run.status}/{conclusion}")
    return "\n".join(lines)


def wait_for_workflows(args: argparse.Namespace, sha: str) -> list[RunInfo]:
    start = time.time()
    initial_deadline = start + args.initial_timeout
    deadline = start + args.timeout
    last_seen: tuple[tuple[int, str, str | None], ...] | None = None

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

        current = fingerprint(runs)
        if current != last_seen:
            print(summarize_runs(runs, sha[:10]), file=sys.stderr)
            last_seen = current

        if all(run.status == "completed" for run in runs):
            return runs

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


def verify_live_state(root: Path, sha: str, runs: list[RunInfo]) -> tuple[dict[str, SurfaceInfo], list[str], str]:
    proc = run([str(root / "scripts" / "ops" / "deploy-status.sh")], cwd=root)
    raw = proc.stdout
    surfaces = parse_deploy_status(raw)
    short_sha = sha[:10]
    workflow_names = {run.workflowName for run in runs}
    errors: list[str] = []

    def require_surface(surface_name: str, allowed_health: set[str]) -> None:
        surface = surfaces.get(surface_name)
        if surface is None:
            errors.append(f"Missing {surface_name!r} in deploy-status output")
            return
        if surface.sha != short_sha:
            errors.append(f"{surface_name} is on {surface.sha}, expected {short_sha}")
        if surface.health not in allowed_health:
            errors.append(f"{surface_name} health is {surface.health}, expected one of {sorted(allowed_health)}")

    if DEPLOY_AND_VERIFY in workflow_names:
        require_surface("Demo runtime", RUNTIME_HEALTH)
        require_surface("Canary (david010)", RUNTIME_HEALTH)

    if DEPLOY_CONTROL_PLANE in workflow_names:
        require_surface("Control plane", CONTROL_PLANE_HEALTH)

    return surfaces, errors, raw


def emit_json(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> int:
    args = parse_args()
    root = repo_root()
    target_sha = args.sha or resolve_head_sha(root)
    target_sha = target_sha.strip()
    short_sha = target_sha[:10]

    try:
        runs = wait_for_workflows(args, target_sha)
    except NoRunsError as exc:
        message = str(exc)
        payload = {
            "repo": args.repo,
            "target_sha": target_sha,
            "result": "no_runs",
            "message": message,
            "workflows": [],
        }
        if args.json:
            emit_json(payload)
        else:
            print(message, file=sys.stderr)
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

    workflow_payload = [asdict(run) for run in runs]

    if not runs_succeeded(runs):
        failures = failed_runs(runs)
        payload = {
            "repo": args.repo,
            "target_sha": target_sha,
            "result": "workflow_failure",
            "workflows": workflow_payload,
            "failed_workflows": [asdict(run) for run in failures],
        }
        if args.json:
            emit_json(payload)
        else:
            print(f"Push workflows failed for {short_sha}:", file=sys.stderr)
            for run in failures:
                conclusion = run.conclusion or "unknown"
                print(f"  - {run.workflowName} #{run.databaseId}: {conclusion}", file=sys.stderr)
                print(f"    {run.url}", file=sys.stderr)
                print(f"    Inspect logs: gh run view {run.databaseId} --log-failed", file=sys.stderr)
        return EXIT_WORKFLOW_FAILURE

    live_surfaces: dict[str, dict] | None = None
    live_errors: list[str] = []
    live_output = ""
    if not args.skip_live:
        surfaces, live_errors, live_output = verify_live_state(root, target_sha, runs)
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
    }
    if live_surfaces is not None:
        payload["live"] = live_surfaces

    if args.json:
        emit_json(payload)
    else:
        print(f"Ship verification passed for {short_sha}.", file=sys.stderr)
        if live_output:
            print("", file=sys.stderr)
            print(live_output.rstrip(), file=sys.stderr)
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
