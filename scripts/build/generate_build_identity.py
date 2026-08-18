#!/usr/bin/env python3
"""Generate .build/build-identity.json — the single source of truth for build identity.

Output lands at .build/build-identity.json relative to the repo root and is
gitignored. Development generation is content-stable: a repeated invocation
for the same semantic identity does not rewrite the file or change its build
timestamp.

Fields:
- version:      release semver (from server/pyproject.toml)
- commit:       full git SHA (GITHUB_SHA in CI, else `git rev-parse HEAD`)
- commit_short: first 8 chars of commit
- dirty:        true iff tracked files differ from HEAD
                (`git diff --quiet HEAD`). Untracked files ignored —
                shared-worktree reality means other agents routinely
                have WIP in the same directory.
- built_at:     UTC ISO 8601 timestamp
- channel:      "release" for tagged CI builds (GITHUB_REF=refs/tags/v*),
                "dev" for everything else.

Exit non-zero if we cannot determine any field — fail loudly, never
paper over a broken provenance.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / ".build" / "build-identity.json"
PYPROJECT_PATH = REPO_ROOT / "server" / "pyproject.toml"
# Python loads identity as a package resource. The generator stages it
# directly into the source tree so editable installs, source runs, and
# wheels all read the same file with no env-var fallback.
PYTHON_PACKAGE_OUTPUT = REPO_ROOT / "server" / "zerg" / "build_identity.json"

VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
# refs/tags/vX.Y.Z, optionally followed by -prerelease (rc1, beta.2, etc.) or +buildmeta.
TAG_REF_RE = re.compile(r"^refs/tags/v\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")
SEMANTIC_KEYS = ("version", "commit", "commit_short", "dirty", "channel")


def read_version(pyproject_path: Path) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise RuntimeError(f"no version line found in {pyproject_path}")
    return match.group(1)


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def resolve_commit(repo_root: Path, env: dict[str, str]) -> str:
    sha = env.get("GITHUB_SHA", "").strip()
    if sha:
        return sha
    return _git(["rev-parse", "HEAD"], cwd=repo_root)


def resolve_dirty(repo_root: Path, env: dict[str, str]) -> bool:
    # Tracked-only: staged, unstaged, and deleted files all count;
    # untracked files do not (shared-worktree reality — other agents
    # routinely have WIP in the same directory).
    # Not CI-special: a CI build that has somehow mutated tracked files
    # before packaging should surface as dirty — that is a real
    # provenance red flag, not something to paper over.
    del env  # currently unused; kept for future CI-specific tweaks
    proc = subprocess.run(
        ["git", "diff", "--quiet", "HEAD"],
        cwd=repo_root,
    )
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    raise RuntimeError(f"git diff --quiet HEAD failed (rc={proc.returncode})")


def resolve_channel(env: dict[str, str]) -> str:
    ref = env.get("GITHUB_REF", "")
    if TAG_REF_RE.match(ref):
        return "release"
    return "dev"


def build_identity(
    *,
    repo_root: Path = REPO_ROOT,
    pyproject_path: Path = PYPROJECT_PATH,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    env = env if env is not None else os.environ.copy()
    now = now or datetime.now(timezone.utc)

    version = read_version(pyproject_path)
    commit = resolve_commit(repo_root, env)
    if len(commit) < 8:
        raise RuntimeError(f"commit sha too short: {commit!r}")
    dirty = resolve_dirty(repo_root, env)
    channel = resolve_channel(env)

    return {
        "version": version,
        "commit": commit,
        "commit_short": commit[:8],
        "dirty": dirty,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channel": channel,
    }


def _format_timestamp(value: str) -> str:
    """Normalize an ISO timestamp or Unix epoch to the stored UTC format."""
    raw = value.strip()
    if not raw:
        raise RuntimeError("build timestamp cannot be empty")
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RuntimeError(f"build timestamp must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit_timestamp(repo_root: Path, commit: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", "-s", "--format=%cI", commit],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return _format_timestamp(proc.stdout)


def _read_identity(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _same_semantic_identity(left: dict | None, right: dict) -> bool:
    return bool(left) and all(left.get(key) == right.get(key) for key in SEMANTIC_KEYS)


def _resolve_built_at(
    *,
    identity: dict,
    existing: dict | None,
    repo_root: Path,
    env: dict[str, str],
    explicit: str | None,
    now: datetime,
) -> str:
    requested = explicit or env.get("LONGHOUSE_BUILD_TIMESTAMP") or env.get("SOURCE_DATE_EPOCH")
    if requested:
        return _format_timestamp(requested)

    if _same_semantic_identity(existing, identity):
        prior = existing.get("built_at")
        if isinstance(prior, str) and prior.strip():
            return prior

    # Separate release jobs need one deterministic value even when they start
    # at different wall-clock times. An explicit timestamp wins; otherwise use
    # the tagged commit timestamp as the release authority.
    if identity["channel"] == "release":
        from_commit = _git_commit_timestamp(repo_root, identity["commit"])
        if from_commit:
            return from_commit

    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def identity_lock(repo_root: Path):
    """Serialize identity generation across all writers in this worktree."""
    lock_path = repo_root / ".build" / "locks" / "build-identity.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _serialized_identity(identity: dict) -> bytes:
    return (json.dumps(identity, indent=2) + "\n").encode("utf-8")


def write_identity(identity: dict, output_path: Path) -> bool:
    """Atomically write identity if bytes changed; return whether it changed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _serialized_identity(identity)
    try:
        if output_path.read_bytes() == payload:
            return False
    except FileNotFoundError:
        pass

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output_path)
        return True
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    default_output = REPO_ROOT / ".build" / "build-identity.json"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help="output path (default: .build/build-identity.json at repo root)",
    )
    parser.add_argument(
        "--pyproject-path",
        type=Path,
        default=PYPROJECT_PATH,
        help="pyproject.toml to read version from (default: server/pyproject.toml)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="also print the JSON to stdout",
    )
    parser.add_argument(
        "--skip-python-package",
        action="store_true",
        help="skip writing the staged copy under server/zerg/build_identity.json "
        "(used by unit tests that pass a custom --output in a temp repo).",
    )
    parser.add_argument(
        "--built-at",
        default=None,
        help="explicit ISO-8601 or Unix-epoch build timestamp (release automation)",
    )
    args = parser.parse_args(argv)

    with identity_lock(REPO_ROOT):
        identity = build_identity(repo_root=REPO_ROOT, pyproject_path=args.pyproject_path)
        existing = _read_identity(args.output)
        identity["built_at"] = _resolve_built_at(
            identity=identity,
            existing=existing,
            repo_root=REPO_ROOT,
            env=os.environ,
            explicit=args.built_at,
            now=datetime.now(timezone.utc),
        )
        write_identity(identity, args.output)
        if not args.skip_python_package:
            # Stage the same bytes into the Python package tree so
            # `importlib.resources.files("zerg") / "build_identity.json"` always
            # resolves, regardless of install mode (editable, wheel, docker).
            write_identity(identity, PYTHON_PACKAGE_OUTPUT)
    if args.print:
        print(json.dumps(identity, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
