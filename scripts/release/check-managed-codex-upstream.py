#!/usr/bin/env python3
"""Inspect upstream Codex releases/tags for managed runtime automation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILD_SCRIPT_PATH = Path(__file__).resolve().with_name("build-managed-codex.sh")
PATCH_FILE_PATH = Path(__file__).resolve().with_name("managed-codex.patch")
UPSTREAM_REPO_URL = "https://github.com/openai/codex.git"
UPSTREAM_API_ROOT = "https://api.github.com/repos/openai/codex"
UPSTREAM_ISSUE_URL = "https://github.com/openai/codex/issues/18203"

DEFAULT_UPSTREAM_REF_RE = re.compile(r'^DEFAULT_UPSTREAM_REF="([^"]+)"$', re.MULTILINE)
DEFAULT_UPSTREAM_VERSION_RE = re.compile(r'^DEFAULT_UPSTREAM_VERSION="([^"]+)"$', re.MULTILINE)
UPSTREAM_TAG_RE = re.compile(r"^rust-v(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
SEMVER_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


@dataclass(frozen=True)
class SemVersion:
    raw: str
    major: int
    minor: int
    patch: int
    prerelease: tuple[tuple[int, int | str], ...]

    @classmethod
    def parse(cls, value: str) -> "SemVersion":
        match = SEMVER_RE.fullmatch(value.strip())
        if not match:
            raise ValueError(f"Unsupported semver: {value!r}")

        prerelease_raw = match.group("pre")
        prerelease: list[tuple[int, int | str]] = []
        if prerelease_raw:
            for part in prerelease_raw.split("."):
                if part.isdigit():
                    prerelease.append((0, int(part)))
                else:
                    prerelease.append((1, part))

        return cls(
            raw=value,
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple(prerelease),
        )

    def sort_key(self) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
        stable_rank = 1 if not self.prerelease else 0
        return (self.major, self.minor, self.patch, stable_rank, self.prerelease)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVersion):
            return NotImplemented
        return self.sort_key() < other.sort_key()


@dataclass(frozen=True)
class UpstreamTag:
    name: str
    version: SemVersion
    ref_sha: str
    commit_sha: str


def _require_git() -> None:
    if shutil.which("git"):
        return
    raise SystemExit("git is required for tag inspection and patch checks")


def _read_build_defaults(build_script_path: Path) -> tuple[str, str]:
    payload = build_script_path.read_text(encoding="utf-8")
    ref_match = DEFAULT_UPSTREAM_REF_RE.search(payload)
    version_match = DEFAULT_UPSTREAM_VERSION_RE.search(payload)
    if not ref_match or not version_match:
        raise SystemExit(f"Unable to parse managed Codex defaults from {build_script_path}")
    return ref_match.group(1), version_match.group(1)


def _fetch_json(url: str, github_token: str | None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "longhouse-managed-codex-upstream-check",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API request failed for {url}: {exc.code} {detail}") from exc


def _list_upstream_tags() -> list[UpstreamTag]:
    completed = subprocess.run(
        ["git", "ls-remote", "--tags", UPSTREAM_REPO_URL],
        check=True,
        capture_output=True,
        text=True,
    )

    refs: dict[str, dict[str, str]] = {}
    for line in completed.stdout.splitlines():
        sha, ref = line.split("\t", 1)
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/") :]
        peeled = name.endswith("^{}")
        base_name = name[:-3] if peeled else name
        entry = refs.setdefault(base_name, {})
        if peeled:
            entry["commit_sha"] = sha
        else:
            entry["ref_sha"] = sha

    tags: list[UpstreamTag] = []
    for name, shas in refs.items():
        match = UPSTREAM_TAG_RE.fullmatch(name)
        if not match:
            continue
        version = SemVersion.parse(match.group("version"))
        ref_sha = shas.get("ref_sha") or shas.get("commit_sha")
        commit_sha = shas.get("commit_sha") or shas.get("ref_sha")
        if not ref_sha or not commit_sha:
            continue
        tags.append(
            UpstreamTag(
                name=name,
                version=version,
                ref_sha=ref_sha,
                commit_sha=commit_sha,
            )
        )

    if not tags:
        raise SystemExit("No upstream rust-v* Codex tags were found")
    return sorted(tags, key=lambda item: item.version.sort_key(), reverse=True)


def _check_patch_status(candidate_tag: UpstreamTag, patch_file_path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="longhouse-codex-upstream-check-") as temp_dir:
        worktree = Path(temp_dir) / "src"
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", candidate_tag.name, UPSTREAM_REPO_URL, str(worktree)],
            check=True,
            capture_output=True,
            text=True,
        )

        forward = subprocess.run(
            ["git", "-C", str(worktree), "apply", "--check", str(patch_file_path)],
            capture_output=True,
            text=True,
        )
        reverse = subprocess.run(
            ["git", "-C", str(worktree), "apply", "--reverse", "--check", str(patch_file_path)],
            capture_output=True,
            text=True,
        )

    if reverse.returncode == 0 and forward.returncode != 0:
        return "already_upstream_or_equivalent"
    if forward.returncode == 0 and reverse.returncode != 0:
        return "applies_cleanly"
    if forward.returncode == 0 and reverse.returncode == 0:
        return "ambiguous"
    return "conflicts"


def _release_summary(release: dict[str, Any] | None) -> dict[str, Any] | None:
    if not release:
        return None
    return {
        "name": release.get("name"),
        "tag_name": release.get("tag_name"),
        "html_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "prerelease": bool(release.get("prerelease")),
        "draft": bool(release.get("draft")),
    }


def _recommendation(*, update_needed: bool, patch_status: str) -> str:
    if not update_needed:
        return "No upstream tag is newer than the currently pinned managed Codex version."
    if patch_status == "already_upstream_or_equivalent":
        return (
            "The carried patch appears to be upstream already. Run the live remote-backpressure probe "
            "against stock upstream, then consider dropping the fork patch."
        )
    if patch_status == "applies_cleanly":
        return (
            "The carried patch still applies on the candidate tag. Run the live remote-backpressure probe "
            "against stock upstream; if the bug still reproduces, rebuild and ship the patched fork."
        )
    if patch_status == "conflicts":
        return (
            "The carried patch no longer applies cleanly. Treat this as a manual rebase/investigation event "
            "before attempting a managed Codex ship."
        )
    return (
        "Patch applicability is ambiguous. Run the live probe and inspect the transport diff before deciding "
        "whether to ship patched or unpatched."
    )


def _agent_prompt(
    *,
    pinned_ref: str,
    pinned_version: str,
    candidate_tag: UpstreamTag,
    patch_status: str,
    candidate_release: dict[str, Any] | None,
) -> str:
    release_body = (candidate_release or {}).get("body") or "(No published release body for this tag.)"
    published_at = (candidate_release or {}).get("published_at") or "unknown"
    release_name = (candidate_release or {}).get("name") or candidate_tag.name
    release_url = (candidate_release or {}).get("html_url") or f"https://github.com/openai/codex/releases/tag/{candidate_tag.name}"
    return textwrap.dedent(
        f"""\
        Review this upstream Codex candidate for Longhouse managed-Codex automation.

        Current Longhouse state:
        - pinned upstream version: {pinned_version}
        - pinned upstream ref: {pinned_ref}
        - carried issue: {UPSTREAM_ISSUE_URL}
        - carried patch status against candidate: {patch_status}

        Candidate upstream:
        - tag: {candidate_tag.name}
        - version: {candidate_tag.version.raw}
        - commit: {candidate_tag.commit_sha}
        - release name: {release_name}
        - release published at: {published_at}
        - release url: {release_url}

        Release notes:
        {release_body}

        Tasks:
        1. Identify any release-note items likely to affect app-server, websocket transport, remote TUI, thread streaming, backpressure, or connection lifecycle.
        2. Assess whether this candidate likely fixes or changes issue #18203.
        3. Recommend one of: keep shipping patched fork, ship unpatched upstream, or require manual investigation.
        4. Call out any specific paths/files or code areas worth diffing first in openai/codex.

        Return compact markdown with:
        - Verdict
        - Evidence
        - Paths to inspect
        - Recommended next action
        """
    ).strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-script",
        type=Path,
        default=BUILD_SCRIPT_PATH,
        help="Path to scripts/release/build-managed-codex.sh",
    )
    parser.add_argument(
        "--patch-file",
        type=Path,
        default=PATCH_FILE_PATH,
        help="Path to scripts/release/managed-codex.patch",
    )
    parser.add_argument(
        "--candidate-tag",
        help="Explicit upstream rust-v* tag to inspect instead of auto-selecting the newest semver tag",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="Optional GitHub token for release API requests (defaults to GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--write-agent-prompt",
        type=Path,
        help="Optional path to write an agent-ready advisory prompt",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a plain-text summary",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    _require_git()

    pinned_ref, pinned_version = _read_build_defaults(args.build_script)
    pinned_semver = SemVersion.parse(pinned_version)

    tags = _list_upstream_tags()
    tag_by_name = {tag.name: tag for tag in tags}
    if args.candidate_tag:
        candidate_tag = tag_by_name.get(args.candidate_tag)
        if not candidate_tag:
            raise SystemExit(f"Candidate tag {args.candidate_tag!r} was not found in upstream tags")
    else:
        candidate_tag = tags[0]

    releases = _fetch_json(f"{UPSTREAM_API_ROOT}/releases?per_page=20", args.github_token or None)
    if not isinstance(releases, list):
        raise SystemExit("Unexpected GitHub releases payload")

    latest_published_release = next((item for item in releases if not item.get("draft") and not item.get("prerelease")), None)
    latest_prerelease = next((item for item in releases if not item.get("draft") and item.get("prerelease")), None)
    candidate_release = next((item for item in releases if item.get("tag_name") == candidate_tag.name), None)

    patch_status = _check_patch_status(candidate_tag, args.patch_file)
    update_needed = pinned_semver < candidate_tag.version
    recommendation = _recommendation(update_needed=update_needed, patch_status=patch_status)
    agent_prompt = _agent_prompt(
        pinned_ref=pinned_ref,
        pinned_version=pinned_version,
        candidate_tag=candidate_tag,
        patch_status=patch_status,
        candidate_release=candidate_release,
    )

    payload = {
        "upstream_repo": UPSTREAM_REPO_URL,
        "pinned_upstream_ref": pinned_ref,
        "pinned_upstream_version": pinned_version,
        "latest_upstream_tag": {
            "name": tags[0].name,
            "version": tags[0].version.raw,
            "commit_sha": tags[0].commit_sha,
        },
        "latest_published_release": _release_summary(latest_published_release),
        "latest_prerelease_release": _release_summary(latest_prerelease),
        "candidate_tag": {
            "name": candidate_tag.name,
            "version": candidate_tag.version.raw,
            "ref_sha": candidate_tag.ref_sha,
            "commit_sha": candidate_tag.commit_sha,
        },
        "candidate_release": _release_summary(candidate_release),
        "update_needed_by_tag": update_needed,
        "patch_status": patch_status,
        "recommendation": recommendation,
        "agent_prompt": agent_prompt,
    }

    if args.write_agent_prompt:
        args.write_agent_prompt.parent.mkdir(parents=True, exist_ok=True)
        args.write_agent_prompt.write_text(agent_prompt + "\n", encoding="utf-8")

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print("Managed Codex upstream check")
    print(f"  pinned upstream version: {pinned_version}")
    print(f"  pinned upstream ref:     {pinned_ref}")
    print(f"  latest upstream tag:     {tags[0].name} ({tags[0].commit_sha[:12]})")
    print(f"  candidate tag:           {candidate_tag.name} ({candidate_tag.commit_sha[:12]})")
    print(f"  update needed by tag:    {str(update_needed).lower()}")
    print(f"  patch status:            {patch_status}")
    if latest_published_release:
        print(
            "  latest full release:     "
            f"{latest_published_release.get('tag_name')} ({latest_published_release.get('published_at')})"
        )
    if latest_prerelease:
        print(
            "  latest prerelease:       "
            f"{latest_prerelease.get('tag_name')} ({latest_prerelease.get('published_at')})"
        )
    print("")
    print(textwrap.fill(recommendation, width=100, initial_indent="Recommendation: ", subsequent_indent=" " * 16))
    if args.write_agent_prompt:
        print(f"Agent prompt: {args.write_agent_prompt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
