#!/usr/bin/env python3
"""Verify that public launch surfaces all point at one exact Longhouse build."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ACCEPTED_CONCLUSIONS = {"success", "neutral", "skipped"}
DEFAULT_REQUIRED_WORKFLOWS = (
    "CI",
    "Deploy and Verify",
    "Launch Gate",
    "Installer Validation Ring",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"{' '.join(cmd)} failed: {detail}")
    return proc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_canary_subdomain = os.environ.get("LONGHOUSE_DEFAULT_SUBDOMAIN") or "david010"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", help="Target commit SHA. Defaults to git HEAD.")
    parser.add_argument("--repo", default="cipher982/longhouse", help="GitHub repo in OWNER/REPO form.")
    parser.add_argument(
        "--required-workflow",
        action="append",
        dest="required_workflows",
        help="Required exact-SHA workflow. May be repeated. Defaults to launch-critical workflows.",
    )
    parser.add_argument("--demo-url", default="https://longhouse.ai/api/health")
    parser.add_argument(
        "--canary-url",
        default=f"https://{default_canary_subdomain}.longhouse.ai/api/health",
    )
    parser.add_argument("--skip-workflows", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--skip-release", action="store_true")
    parser.add_argument("--skip-public-package", action="store_true")
    parser.add_argument("--wait", action="store_true", help="Poll until every check passes or timeout elapses.")
    parser.add_argument("--timeout", type=int, default=3600, help="Wait timeout in seconds. Default: 3600.")
    parser.add_argument("--poll", type=int, default=30, help="Wait poll interval in seconds. Default: 30.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args(argv)


def resolve_sha(root: Path, rev: str | None) -> str:
    target = rev or "HEAD"
    return run(["git", "rev-parse", "--verify", f"{target}^{{commit}}"], cwd=root).stdout.strip()


def commit_matches(actual: str | None, expected: str) -> bool:
    actual = (actual or "").strip()
    expected = expected.strip()
    return bool(actual and expected and (actual == expected or actual.startswith(expected) or expected.startswith(actual)))


def latest_run_by_workflow(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run_info in runs:
        workflow = str(run_info.get("workflowName") or "")
        if not workflow:
            continue
        current = latest.get(workflow)
        if current is None or int(run_info.get("databaseId") or 0) > int(current.get("databaseId") or 0):
            latest[workflow] = run_info
    return latest


def check_workflows(repo: str, sha: str, required: tuple[str, ...]) -> list[Check]:
    proc = run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--commit",
            sha,
            "--limit",
            "100",
            "--json",
            "databaseId,workflowName,status,conclusion,headSha,url,event",
        ]
    )
    runs = [
        item
        for item in json.loads(proc.stdout or "[]")
        if item.get("headSha") == sha
    ]
    latest = latest_run_by_workflow(runs)

    checks: list[Check] = []
    for workflow in required:
        run_info = latest.get(workflow)
        if run_info is None:
            checks.append(Check(f"workflow:{workflow}", False, "no exact-SHA run found"))
            continue
        status = run_info.get("status")
        conclusion = run_info.get("conclusion")
        run_id = run_info.get("databaseId")
        url = run_info.get("url")
        ok = status == "completed" and conclusion in ACCEPTED_CONCLUSIONS
        checks.append(
            Check(
                f"workflow:{workflow}",
                ok,
                f"run {run_id} {status}/{conclusion or '-'} {url}",
            )
        )
    return checks


def fetch_json_url(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "longhouse-launch-readiness"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} returned a non-object JSON payload")
    return payload


def check_live_surface(name: str, url: str, sha: str) -> Check:
    try:
        payload = fetch_json_url(url)
    except Exception as exc:
        return Check(f"live:{name}", False, f"{url} unreachable: {exc}")
    build = payload.get("build")
    if not isinstance(build, dict):
        return Check(f"live:{name}", False, f"{url} response missing build object")
    commit = str(build.get("commit") or "")
    status = str(payload.get("status") or "")
    ok = commit_matches(commit, sha) and status in {"ok", "healthy", "degraded"}
    return Check(
        f"live:{name}",
        ok,
        f"status={status or '<missing>'} commit={commit or '<missing>'} url={url}",
    )


def latest_release(repo: str) -> tuple[str, str]:
    release = run(["gh", "release", "view", "-R", repo, "--json", "tagName"])
    tag = json.loads(release.stdout)["tagName"]
    commit = run(["gh", "api", f"repos/{repo}/commits/{tag}", "--jq", ".sha"]).stdout.strip()
    return tag, commit


def check_latest_release(repo: str, sha: str) -> tuple[Check, str | None]:
    try:
        tag, commit = latest_release(repo)
    except Exception as exc:
        return Check("release:latest", False, f"could not resolve latest release: {exc}"), None
    ok = commit_matches(commit, sha)
    return Check("release:latest", ok, f"{tag} commit={commit}"), tag


def check_public_package(tag: str, sha: str) -> Check:
    version = tag.removeprefix("v")
    proc = run(
        [
            "uv",
            "run",
            "--no-project",
            "--isolated",
            "--with",
            f"longhouse=={version}",
            "longhouse",
            "version",
            "--json",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return Check("package:pypi", False, (proc.stderr or proc.stdout).strip())
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return Check("package:pypi", False, f"version command did not emit JSON: {exc}")
    build = payload.get("build") if isinstance(payload, dict) else None
    if not isinstance(build, dict):
        return Check("package:pypi", False, "version payload missing build object")
    commit = str(build.get("commit") or "")
    actual_version = str(build.get("version") or "")
    ok = commit_matches(commit, sha) and actual_version == version
    return Check("package:pypi", ok, f"version={actual_version} commit={commit}")


def print_human(checks: list[Check]) -> None:
    for check in checks:
        prefix = "OK" if check.ok else "FAIL"
        print(f"{prefix} {check.name}: {check.detail}")


def run_checks(args: argparse.Namespace, sha: str, required: tuple[str, ...]) -> list[Check]:
    checks: list[Check] = []
    if not args.skip_workflows:
        checks.extend(check_workflows(args.repo, sha, required))
    if not args.skip_live:
        checks.append(check_live_surface("demo", args.demo_url, sha))
        checks.append(check_live_surface("canary", args.canary_url, sha))
    release_tag: str | None = None
    if not args.skip_release:
        release_check, release_tag = check_latest_release(args.repo, sha)
        checks.append(release_check)
    if not args.skip_public_package and release_tag:
        checks.append(check_public_package(release_tag, sha))
    return checks


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = repo_root()
    sha = resolve_sha(root, args.sha)
    required = tuple(args.required_workflows or DEFAULT_REQUIRED_WORKFLOWS)

    deadline = time.time() + args.timeout
    attempt = 0
    while True:
        attempt += 1
        checks = run_checks(args, sha, required)
        ok = all(check.ok for check in checks)
        if ok or not args.wait or time.time() >= deadline:
            break
        failing = ", ".join(check.name for check in checks if not check.ok) or "unknown"
        print(
            f"Launch readiness pending for {sha[:12]}: {failing}; retrying in {args.poll}s",
            file=sys.stderr,
        )
        time.sleep(args.poll)

    if args.json:
        print(
            json.dumps(
                {
                    "target_sha": sha,
                    "ok": ok,
                    "attempts": attempt,
                    "checks": [check.__dict__ for check in checks],
                },
                indent=2,
            )
        )
    else:
        print(f"Launch readiness for {sha[:12]}")
        print_human(checks)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
