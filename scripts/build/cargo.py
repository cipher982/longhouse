#!/usr/bin/env python3
"""Longhouse Cargo entrypoint with bounded, inspectable build output."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGET_DIR = REPO_ROOT / ".build" / "cargo-target"
LOCK_DIR = REPO_ROOT / ".build" / "locks"
MARKER_NAME = ".longhouse-target.json"
DEFAULT_BUDGET_BYTES = 12 * 1024**3


def target_dir() -> Path:
    configured = os.environ.get("LONGHOUSE_CARGO_TARGET_DIR") or os.environ.get("CARGO_TARGET_DIR")
    path = Path(configured).expanduser() if configured else DEFAULT_TARGET_DIR
    if not path.is_absolute():
        path = REPO_ROOT / path
    # Keep the final path component visible so symlinked targets can be
    # rejected instead of silently following a link into an unrelated tree.
    return Path(os.path.abspath(path))


def _lock_path(target: Path) -> Path:
    key = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:20]
    return LOCK_DIR / f"cargo-{key}.lock"


@contextlib.contextmanager
def target_lock(target: Path):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with _lock_path(target).open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _budget_bytes() -> int:
    raw = os.environ.get("LONGHOUSE_CARGO_TARGET_BUDGET_GB", "12")
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid LONGHOUSE_CARGO_TARGET_BUDGET_GB={raw!r}") from exc
    if value <= 0:
        raise SystemExit("LONGHOUSE_CARGO_TARGET_BUDGET_GB must be positive")
    return int(value * 1024**3)


def _size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    proc = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, check=True)
    return int(proc.stdout.split()[0]) * 1024


def _human_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.1f}{unit}"
        number /= 1024
    return f"{value}B"


def _incremental_stats(target: Path) -> tuple[int, float | None, float | None]:
    directories: list[Path] = []
    for profile in ("debug", "release", "ci"):
        incremental = target / profile / "incremental"
        if incremental.is_dir() and not incremental.is_symlink():
            directories.extend(path for path in incremental.iterdir() if path.is_dir())
    if not directories:
        return 0, None, None
    mtimes = [path.stat().st_mtime for path in directories]
    return len(directories), min(mtimes), max(mtimes)


def _marker_path(target: Path) -> Path:
    return target / MARKER_NAME


def _reject_symlinked_path(target: Path) -> None:
    if target.is_symlink():
        raise SystemExit(f"refusing symlinked Cargo target path: {target}")


def _ensure_marker(target: Path) -> None:
    _reject_symlinked_path(target)
    target.mkdir(parents=True, exist_ok=True)
    marker = _marker_path(target)
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid Longhouse Cargo target marker: {marker}") from exc
        if payload.get("repo_root") != str(REPO_ROOT):
            raise SystemExit(f"Cargo target belongs to another checkout: {target}")
        return
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "repo_root": str(REPO_ROOT),
                "target_dir": str(target),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _profile_dir(profile: str) -> str:
    return "debug" if profile in {"dev", "test"} else profile


def artifact_path(profile: str, binary: str, target: str | None = None) -> Path:
    base = target_dir()
    if target:
        base = base / target
    return base / _profile_dir(profile) / binary


def _health(*, fail_over_budget: bool) -> int:
    target = target_dir()
    if target.exists() or target.is_symlink():
        _reject_symlinked_path(target)
    total = _size_bytes(target)
    budget = _budget_bytes()
    print(f"cargo target: {target}")
    print(f"cargo target size: {_human_bytes(total)} / {_human_bytes(budget)} budget")
    if target.exists():
        for profile in ("debug", "release", "ci"):
            profile_path = target / profile
            if profile_path.exists():
                print(f"  {profile}: {_human_bytes(_size_bytes(profile_path))}")
        count, oldest, newest = _incremental_stats(target)
        if oldest is None or newest is None:
            print("  incremental: 0 directories")
        else:
            now = time.time()
            oldest_age = max(0.0, (now - oldest) / 86400)
            newest_age = max(0.0, (now - newest) / 86400)
            oldest_at = datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat()
            newest_at = datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
            print(
                f"  incremental: {count} directories; "
                f"oldest {oldest_age:.1f}d ({oldest_at}); "
                f"newest {newest_age:.1f}d ({newest_at})"
            )
    if total > budget:
        print(
            f"ERROR: Cargo target exceeds budget; run `python3 scripts/build/cargo.py clean`.",
            file=sys.stderr,
        )
        return 2 if fail_over_budget else 0
    return 0


def health(*, fail_over_budget: bool) -> int:
    with target_lock(target_dir()):
        return _health(fail_over_budget=fail_over_budget)


def clean() -> int:
    target = target_dir()
    with target_lock(target):
        if not target.exists():
            if target.is_symlink():
                raise SystemExit(f"refusing to clean symlinked Cargo target directory: {target}")
            print(f"Cargo target already absent: {target}")
            return 0
        if target.is_symlink():
            raise SystemExit(f"refusing to clean symlinked Cargo target directory: {target}")
        marker = _marker_path(target)
        if not marker.exists():
            raise SystemExit(f"refusing to clean unmarked Cargo target directory: {target}")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"invalid Longhouse Cargo target marker: {marker}") from exc
        if payload.get("repo_root") != str(REPO_ROOT):
            raise SystemExit(f"refusing to clean target owned by another checkout: {target}")

        trash = target.with_name(f"{target.name}.deleting-{uuid.uuid4().hex}")
        target.rename(trash)
        try:
            shutil.rmtree(trash)
        except Exception:
            # Preserve a recoverable path if deletion is interrupted.
            if trash.exists() and not target.exists():
                trash.rename(target)
            raise
    print(f"cleaned Cargo target: {target}")
    return 0


def run_cargo(cargo_args: list[str]) -> int:
    while cargo_args and cargo_args[0] == "--":
        cargo_args = cargo_args[1:]
    if not cargo_args:
        raise SystemExit("usage: cargo.py exec -- <cargo arguments>")

    target = target_dir()
    with target_lock(target):
        _ensure_marker(target)
        if _health(fail_over_budget=True) != 0:
            return 2
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = str(target)
        if env.get("LONGHOUSE_CARGO_DEBUG") == "full":
            env.setdefault("CARGO_PROFILE_DEV_DEBUG", "2")
            env.setdefault("CARGO_PROFILE_TEST_DEBUG", "2")
        result = subprocess.run(["cargo", *cargo_args], env=env)
        if result.returncode == 0:
            _health(fail_over_budget=False)
        return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("target-dir", help="print the resolved Cargo target directory")
    subparsers.add_parser("health", help="report target size and profile sizes")
    subparsers.add_parser("clean", help="remove the marked target directory")

    artifact = subparsers.add_parser("artifact", help="print a built binary path")
    artifact.add_argument("--profile", required=True)
    artifact.add_argument("--bin", required=True, dest="binary")
    artifact.add_argument("--target", default=None)

    execute = subparsers.add_parser("exec", help="run Cargo with the managed target directory")
    execute.add_argument("cargo_args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.command == "target-dir":
        print(target_dir())
        return 0
    if args.command == "health":
        return health(fail_over_budget=True)
    if args.command == "clean":
        return clean()
    if args.command == "artifact":
        print(artifact_path(args.profile, args.binary, args.target))
        return 0
    return run_cargo(args.cargo_args)


if __name__ == "__main__":
    raise SystemExit(main())
