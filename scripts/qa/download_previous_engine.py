#!/usr/bin/env python3
"""Download the previously released longhouse-engine binary for this platform.

Backs the engine/server compatibility smoke (release-rings.md change 5): CI
proves the candidate server still accepts the engine that is actually running
on users' laptops right now, which is the newest published GitHub release
that is *not* the candidate's own version -- the candidate's own version tag
usually already exists (release.sh bumps and tags main directly), so "newest
release" alone would just download the candidate again.

Exit codes:
  0  binary downloaded to --output
  3  skip -- no previous release exists for this platform (not a failure;
     callers should treat this as "nothing to test", not an error)
  1  hard failure (missing `gh`, network/API error, auth failure, ...)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "server" / "pyproject.toml"
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
SKIP = 3

# Release asset names, from .github/workflows/local-runtime-release.yml /
# scripts/ops/release.sh. Only the platforms that release.sh actually ships.
PLATFORM_ASSETS = {
    "darwin-arm64": "longhouse-engine-darwin-arm64",
    "linux-arm64": "longhouse-engine-linux-arm64",
    "linux-x64": "longhouse-engine-linux-x64",
}


class Skip(Exception):
    """No previous release exists for this platform; not an error."""


def read_candidate_version(pyproject_path: Path) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise RuntimeError(f"no version line found in {pyproject_path}")
    return match.group(1)


def detect_platform() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    x64 = machine in ("x86_64", "amd64")
    if system == "Darwin" and arm:
        return "darwin-arm64"
    if system == "Linux" and arm:
        return "linux-arm64"
    if system == "Linux" and x64:
        return "linux-x64"
    raise Skip(f"no released engine asset for platform {system}/{machine}")


def gh_json(args: list[str]) -> object:
    proc = subprocess.run(
        ["gh", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed (rc={proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    return json.loads(proc.stdout)


def previous_release_tag(*, repo: str, candidate_version: str) -> str:
    """Newest published, non-prerelease release whose tag isn't the candidate's own version."""
    releases = gh_json(
        [
            "release",
            "list",
            "--repo",
            repo,
            "--exclude-drafts",
            "--exclude-pre-releases",
            "--json",
            "tagName,publishedAt",
            "--limit",
            "30",
        ]
    )
    if not isinstance(releases, list):
        raise RuntimeError(f"unexpected `gh release list` output: {releases!r}")

    candidate_tag = f"v{candidate_version}"
    eligible = [
        r for r in releases if isinstance(r.get("tagName"), str) and STABLE_TAG_RE.match(r["tagName"]) and r["tagName"] != candidate_tag
    ]
    if not eligible:
        raise Skip(f"no published release of {repo} precedes the candidate ({candidate_tag}); nothing to compat-test yet")
    eligible.sort(key=lambda r: r.get("publishedAt") or "", reverse=True)
    return eligible[0]["tagName"]


def download_asset(*, repo: str, tag: str, asset_name: str, output: Path) -> None:
    assets = gh_json(["release", "view", tag, "--repo", repo, "--json", "assets"])
    names = {a.get("name") for a in assets.get("assets", [])} if isinstance(assets, dict) else set()
    if asset_name not in names:
        raise Skip(f"release {tag} of {repo} has no {asset_name} asset")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    proc = subprocess.run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repo,
            "--pattern",
            asset_name,
            "--output",
            str(output),
            "--clobber",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh release download {tag} --pattern {asset_name} failed (rc={proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    mode = output.stat().st_mode
    output.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / ".build" / "engine-compat" / "longhouse-engine",
        help="Where to write the downloaded engine binary.",
    )
    parser.add_argument(
        "--platform",
        choices=sorted(PLATFORM_ASSETS),
        help="Override platform detection (darwin-arm64, linux-arm64, linux-x64).",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "cipher982/longhouse"),
        help="owner/repo to read releases from (default: %(default)s).",
    )
    args = parser.parse_args()

    if shutil.which("gh") is None:
        print("download_previous_engine: `gh` not found on PATH", file=sys.stderr)
        return 1

    try:
        engine_platform = args.platform or detect_platform()
        asset_name = PLATFORM_ASSETS[engine_platform]
        candidate_version = read_candidate_version(PYPROJECT_PATH)
        tag = previous_release_tag(repo=args.repo, candidate_version=candidate_version)
        download_asset(repo=args.repo, tag=tag, asset_name=asset_name, output=args.output)
    except Skip as skip:
        print(f"SKIP: {skip}", file=sys.stderr)
        return SKIP

    print(f"engine-compat: downloaded {asset_name} from {tag} -> {args.output}")
    print(f"TAG={tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
