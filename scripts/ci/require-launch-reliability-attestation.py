#!/usr/bin/env python3
"""Require a valid source-bound launch-reliability receipt for a stable release."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from typing import Callable

DEFAULT_WORKFLOW = "launch-reliability-attestation.yml"
DEFAULT_API_URL = "https://api.github.com"
RECEIPT_NAME = "launch-reliability-attestation.json"
SUBJECT_NAME = "trusted-launch-reliability-attestation-subject.json"


class GitHubAPI:
    def __init__(self, token: str, *, api_url: str = DEFAULT_API_URL):
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _request(self, path: str, *, accept: str) -> bytes:
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "longhouse-release-attestation-gate",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"GitHub API {path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub API request failed for {path}: {exc}") from exc

    def json(self, path: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._request(path, accept="application/vnd.github+json"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GitHub API returned invalid JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"GitHub API returned a non-object for {path}")
        return payload

    def bytes(self, path: str) -> bytes:
        return self._request(path, accept="application/vnd.github+json")


def _resolve_tag_sha(api: GitHubAPI, repository: str, tag: str) -> str:
    encoded_tag = urllib.parse.quote(tag, safe="")
    ref = api.json(f"/repos/{repository}/git/ref/tags/{encoded_tag}")
    target = ref.get("object")
    if not isinstance(target, dict) or not isinstance(target.get("sha"), str):
        raise RuntimeError(f"Git tag {tag!r} has no resolvable object")
    if target.get("type") == "commit":
        return target["sha"]
    if target.get("type") == "tag":
        annotated = api.json(f"/repos/{repository}/git/tags/{target['sha']}")
        target = annotated.get("object")
        if not isinstance(target, dict) or target.get("type") != "commit":
            raise RuntimeError(f"Annotated Git tag {tag!r} does not point to a commit")
        commit_sha = target.get("sha")
        if not isinstance(commit_sha, str):
            raise RuntimeError(f"Annotated Git tag {tag!r} has no commit SHA")
        return commit_sha
    raise RuntimeError(f"Git tag {tag!r} has unsupported object type {target.get('type')!r}")


def _successful_runs(api: GitHubAPI, repository: str, workflow: str) -> list[dict[str, Any]]:
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    runs: list[dict[str, Any]] = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"status": "success", "per_page": 100, "page": page})
        payload = api.json(f"/repos/{repository}/actions/workflows/{encoded_workflow}/runs?{query}")
        page_runs = payload.get("workflow_runs")
        if not isinstance(page_runs, list):
            raise RuntimeError("GitHub workflow-run response has no workflow_runs list")
        runs.extend(run for run in page_runs if isinstance(run, dict))
        if len(page_runs) < 100:
            break
    return sorted(
        runs,
        key=lambda run: str(run.get("completed_at") or run.get("created_at") or ""),
        reverse=True,
    )


def _safe_extract(data: bytes, destination: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = destination.resolve()
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"attestation artifact contains an unsafe path: {member.filename!r}")
        archive.extractall(destination)


def _single_named_file(root: Path, name: str) -> Path:
    matches = [path for path in root.rglob(name) if path.is_file() and not path.is_symlink()]
    if len(matches) != 1:
        raise RuntimeError(f"attestation artifact must contain exactly one {name}, found {len(matches)}")
    return matches[0]


def _source_sha(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read attestation subject {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("attestation subject is not a JSON object")
    provenance = payload.get("report_provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("git_sha"), str):
        raise RuntimeError("attestation subject has no report source SHA")
    return provenance["git_sha"]


def find_qualifying_attestation(
    api: GitHubAPI,
    *,
    repository: str,
    release_tag: str,
    workflow: str = DEFAULT_WORKFLOW,
    verifier: Callable[[Path, Path], None] | None = None,
) -> dict[str, Any]:
    """Return the newest successful receipt whose subject binds to release_tag."""

    release_sha = _resolve_tag_sha(api, repository, release_tag)
    rejected: list[str] = []
    for run in _successful_runs(api, repository, workflow):
        run_id = run.get("id")
        if not isinstance(run_id, int):
            continue
        artifacts = api.json(f"/repos/{repository}/actions/runs/{run_id}/artifacts").get("artifacts")
        if not isinstance(artifacts, list):
            rejected.append(f"run {run_id}: artifact response is malformed")
            continue
        expected_name = f"launch-reliability-attestation-{run_id}"
        artifact = next(
            (
                value
                for value in artifacts
                if isinstance(value, dict) and value.get("name") == expected_name
            ),
            None,
        )
        if not isinstance(artifact, dict) or artifact.get("expired") is True:
            continue
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, int):
            rejected.append(f"run {run_id}: attestation artifact has no id")
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="longhouse-attestation-") as temporary:
                root = Path(temporary)
                _safe_extract(
                    api.bytes(f"/repos/{repository}/actions/artifacts/{artifact_id}/zip"),
                    root,
                )
                receipt_path = _single_named_file(root, RECEIPT_NAME)
                subject_path = _single_named_file(root, SUBJECT_NAME)
                subject_sha = _source_sha(subject_path)
                if subject_sha != release_sha:
                    raise RuntimeError(
                        f"subject source {subject_sha} does not match release commit {release_sha}"
                    )
                if verifier is not None:
                    verifier(receipt_path, subject_path)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            rejected.append(f"run {run_id}: {exc}")
            continue
        return {
            "release_tag": release_tag,
            "release_sha": release_sha,
            "run_id": run_id,
            "run_url": run.get("html_url"),
            "artifact_id": artifact_id,
            "artifact_name": expected_name,
        }
    details = "; ".join(rejected[-5:])
    suffix = f" Rejected candidates: {details}" if details else ""
    raise RuntimeError(
        f"no successful launch-reliability attestation is bound to {release_tag} ({release_sha})."
        " Run the protected-main qualification workflow for this exact commit first."
        + suffix
    )


def _verify_with_repository(receipt: Path, subject: Path, repository_root: Path) -> None:
    verifier = repository_root / "scripts/qa/launch-reliability-attestation.py"
    subprocess.run(
        [sys.executable, str(verifier), "verify", "--receipt", str(receipt), "--subject", str(subject)],
        cwd=repository_root,
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", DEFAULT_API_URL))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.token:
        raise SystemExit("launch reliability release gate: GITHUB_TOKEN or GH_TOKEN is required")
    api = GitHubAPI(args.token, api_url=args.api_url)
    try:
        result = find_qualifying_attestation(
            api,
            repository=args.repository,
            release_tag=args.release_tag,
            workflow=args.workflow,
            verifier=lambda receipt, subject: _verify_with_repository(
                receipt, subject, Path(__file__).resolve().parents[2]
            ),
        )
    except RuntimeError as exc:
        raise SystemExit(f"launch reliability release gate: {exc}") from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
