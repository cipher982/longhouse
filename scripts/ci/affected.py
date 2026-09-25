#!/usr/bin/env python3
"""Resolve changed files to CI lanes from ``.github/path-filters.yml``.

This is deliberately a small, local-only resolver.  The path filter file is the
only source of path patterns; this module only describes what each existing
filter means for local/CI execution.  It never contacts GitHub and never
silently turns a failed git operation into an empty change set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[2]
FILTERS_PATH = ROOT / ".github" / "path-filters.yml"


class ResolverError(RuntimeError):
    """An input needed to resolve affected files was unavailable or invalid."""


@dataclass(frozen=True)
class FilterPlan:
    """Execution advice for one path-filter category."""

    lanes: tuple[str, ...]
    commands: tuple[str, ...]
    note: str | None = None
    separate_gates: tuple[str, ...] = ()


# These are lane/command semantics, not path patterns.  Patterns MUST remain in
# .github/path-filters.yml so this local view cannot drift from CI authority.
FILTER_PLANS: dict[str, FilterPlan] = {
    "backend": FilterPlan(
        ("ubuntu-24.04-arm",),
        ("make test",),
        "Backend tests are a full suite (~3.5min serial locally; CI runs three ~1.7min shards).",
    ),
    "frontend": FilterPlan(("ubuntu-24.04-arm",), ("make test-frontend",)),
    "engine": FilterPlan(("ubuntu-24.04-arm",), ("make test-engine",)),
    "schemas": FilterPlan(
        ("ubuntu-24.04-arm",),
        ("make validate",),
        "Validation is cross-cutting; run it explicitly when schema contracts change.",
    ),
    "ci_plumbing": FilterPlan(
        ("ubuntu-24.04-arm",),
        ("make validate",),
        "CI plumbing changes need the full contract validation path.",
    ),
    "packaging": FilterPlan(("ubuntu-24.04-arm",), ("make test-wheel-package",)),
    "runner": FilterPlan(("ubuntu-24.04-arm",), ("make test-runner",)),
    "ios": FilterPlan(
        (),
        ("make ios-unit",),
        "Host-only iOS unit target; the dispatched iOS merge gate is separate.",
        ("test-ios (separate iOS CI gate)",),
    ),
    "e2e": FilterPlan(("ubuntu-24.04-arm",), ("make test-e2e",)),
    "models": FilterPlan(
        ("ubuntu-24.04-arm",),
        (),
        "Model smoke is credentialed CI work; no local target is suggested.",
    ),
    "scripts": FilterPlan(
        ("ubuntu-24.04-arm",),
        ("make test",),
        "Script changes use the backend/helper CI coverage (the full backend suite).",
    ),
    "runtime_image": FilterPlan(
        ("cube-maint",),
        (),
        "Runtime-image assembly is CI-owned; no local deployment is run by this resolver.",
    ),
    "deploy_verify": FilterPlan(
        ("cube-deploy",),
        (),
        "Deploy verification stays on the existing deploy lane and exact-SHA evidence path.",
    ),
}

UNKNOWN_PLAN = FilterPlan(
    ("ubuntu-24.04-arm",),
    (),
    "No path filter matched; review which CI suite should cover it (no local target is run automatically).",
)

# The current authority uses ordinary repository-relative globs.  Rejecting
# minimatch-only constructs is intentional: a newly introduced pattern that
# this local resolver cannot reproduce must fail closed rather than disappear.
_UNSUPPORTED_PATTERN = re.compile(r"(?:^!|\{|\}|\(|\)|\[|\]|\\)")


def _git(root: Path, args: Sequence[str]) -> bytes:
    """Run git and raise with stderr retained instead of returning no paths."""

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ResolverError(f"could not run git: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ResolverError(f"git {' '.join(args)} failed with status {result.returncode}{suffix}")
    return result.stdout


def _nul_paths(payload: bytes) -> list[str]:
    """Decode git's NUL-delimited path output without losing unusual names."""

    return [os.fsdecode(raw) for raw in payload.split(b"\0") if raw]


def _verify_revision(root: Path, revision: str, label: str) -> str:
    """Resolve and verify a commit-ish before asking git for its diff."""

    if not revision.strip():
        raise ResolverError(f"{label} revision is empty")
    try:
        payload = _git(root, ["rev-parse", "--verify", f"{revision}^{{commit}}"])
    except ResolverError as exc:
        raise ResolverError(f"{label} revision {revision!r} is unavailable: {exc}") from exc
    resolved = payload.decode("ascii", "strict").strip()
    if not resolved:
        raise ResolverError(f"{label} revision {revision!r} resolved to an empty value")
    return resolved


def collect_paths(root: Path, base: str, head: str) -> dict[str, list[str]]:
    """Collect committed, staged, unstaged, and nonignored untracked paths."""

    verified_base = _verify_revision(root, base, "BASE")
    verified_head = _verify_revision(root, head, "HEAD")
    committed = _nul_paths(
        _git(root, ["diff", "--name-only", "--no-renames", "-z", verified_base, verified_head])
    )
    staged = _nul_paths(_git(root, ["diff", "--cached", "--name-only", "--no-renames", "-z"]))
    unstaged = _nul_paths(_git(root, ["diff", "--name-only", "--no-renames", "-z"]))
    untracked = _nul_paths(_git(root, ["ls-files", "--others", "--exclude-standard", "-z"]))
    return {
        "committed": sorted(set(committed)),
        "staged": sorted(set(staged)),
        "unstaged": sorted(set(unstaged)),
        "untracked": sorted(set(untracked)),
    }


def _load_filters(path: Path) -> dict[str, tuple[str, ...]]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ResolverError(f"cannot read path filters {path}: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ResolverError(f"path filters {path} must be a non-empty YAML mapping")

    filters: dict[str, tuple[str, ...]] = {}
    for category, patterns in payload.items():
        if not isinstance(category, str) or not category:
            raise ResolverError(f"path filters {path} has a non-string category")
        if category not in FILTER_PLANS:
            raise ResolverError(f"path filters {path} contains unsupported category {category!r}")
        if not isinstance(patterns, list) or not patterns:
            raise ResolverError(f"path filter {category!r} must be a non-empty list")
        normalized: list[str] = []
        for pattern in patterns:
            if (
                not isinstance(pattern, str)
                or not pattern
                or pattern.startswith("/")
                or _UNSUPPORTED_PATTERN.search(pattern)
                or pattern.count("[") != pattern.count("]")
            ):
                raise ResolverError(f"path filter {category!r} contains unsupported pattern {pattern!r}")
            if "\x00" in pattern:
                raise ResolverError(f"path filter {category!r} contains NUL in pattern")
            _compile_pattern(pattern)
            normalized.append(pattern)
        filters[category] = tuple(normalized)
    return filters


@lru_cache(maxsize=None)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile the basic picomatch glob subset used by the authority file."""

    parts: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            # `**/` may consume no directory (e.g. `**/*.py` matches `x.py`).
            parts.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
            continue
        character = pattern[index]
        if character == "*":
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(character))
        index += 1
    parts.append("$")
    try:
        return re.compile("".join(parts))
    except re.error as exc:
        raise ResolverError(f"unsupported path filter pattern {pattern!r}: {exc}") from exc


def _matches(path: str, pattern: str) -> bool:
    # Git path names are POSIX-style.  Unlike fnmatch, a single `*` never
    # crosses a slash, matching dorny/paths-filter's picomatch behavior.
    return _compile_pattern(pattern).fullmatch(path) is not None


def resolve(filters: dict[str, tuple[str, ...]], paths_by_source: dict[str, list[str]]) -> dict[str, Any]:
    """Resolve files to every matching category and its execution advice."""

    all_paths = sorted({path for paths in paths_by_source.values() for path in paths})
    matches: dict[str, list[str]] = {}
    category_paths: dict[str, list[str]] = {category: [] for category in filters}
    unknown_paths: list[str] = []
    for path in all_paths:
        categories = [category for category, patterns in filters.items() if any(_matches(path, pattern) for pattern in patterns)]
        matches[path] = categories
        if not categories:
            unknown_paths.append(path)
        for category in categories:
            category_paths[category].append(path)

    selected_categories = [category for category in filters if category_paths[category]]
    selected_lanes: list[str] = []
    commands: list[dict[str, Any]] = []
    separate_gates: list[str] = []
    notes: list[str] = []
    command_categories: dict[str, list[str]] = {}
    for category in selected_categories:
        plan = FILTER_PLANS[category]
        for lane in plan.lanes:
            if lane not in selected_lanes:
                selected_lanes.append(lane)
        for command in plan.commands:
            command_categories.setdefault(command, []).append(category)
        for gate in plan.separate_gates:
            if gate not in separate_gates:
                separate_gates.append(gate)
        if plan.note and plan.note not in notes:
            notes.append(plan.note)

    if unknown_paths:
        for lane in UNKNOWN_PLAN.lanes:
            if lane not in selected_lanes:
                selected_lanes.append(lane)
        for command in UNKNOWN_PLAN.commands:
            command_categories.setdefault(command, []).append("<unmatched>")
        if UNKNOWN_PLAN.note:
            notes.append(UNKNOWN_PLAN.note)

    for command, categories in command_categories.items():
        commands.append({"command": command, "categories": categories})

    normalized_sources = {source: sorted(set(paths)) for source, paths in paths_by_source.items()}
    committed_diff = normalized_sources.get("committed", [])
    dirty_tree = sorted(
        {
            path
            for source in ("staged", "unstaged", "untracked")
            for path in normalized_sources.get(source, [])
        }
    )
    return {
        "files": sorted(set(committed_diff) | set(dirty_tree)),
        "committed_diff": committed_diff,
        "dirty_tree": dirty_tree,
        "sources": normalized_sources,
        "matches": matches,
        "category_paths": {category: paths for category, paths in category_paths.items() if paths},
        "unknown_paths": unknown_paths,
        "categories": selected_categories,
        "lanes": selected_lanes,
        "commands": commands,
        "separate_gates": separate_gates,
        "notes": notes,
    }


def _human(result: dict[str, Any], *, base: str, head: str) -> str:
    lines = [f"Affected files ({len(result['files'])}; BASE={base}, HEAD={head}):"]
    if result["files"]:
        lines.extend(result["files"])
    else:
        lines.append("(none)")
    source_counts = ", ".join(f"{source}={len(paths)}" for source, paths in result["sources"].items())
    lines.append(f"Change sources (union above): {source_counts}")
    lines.append(f"Committed diff: {len(result['committed_diff'])} path(s)")
    lines.append(f"Dirty tree union: {len(result['dirty_tree'])} path(s)")
    lanes = ", ".join(result["lanes"]) or "(none)"
    lines.append(f"CI lanes: {lanes}")
    lines.append("Local make commands:")
    if result["commands"]:
        for row in result["commands"]:
            categories = ", ".join(row["categories"])
            lines.append(f"  {row['command']} [{categories}]")
    else:
        lines.append("  (none; CI-owned or credentialed work)")
    if result["unknown_paths"]:
        lines.append("Unmatched paths (no filter; review coverage):")
        lines.extend(result["unknown_paths"])
    for gate in result["separate_gates"]:
        lines.append(f"Separate gate: {gate}")
    for note in result["notes"]:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def _run_commands(root: Path, commands: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run only commands represented by explicit supported ``make`` targets."""

    outcomes: list[dict[str, Any]] = []
    for row in commands:
        command = str(row["command"])
        target = command.removeprefix("make ")
        if not target or " " in target or not target.replace("-", "").replace("_", "").isalnum():
            raise ResolverError(f"refusing unsupported local command {command!r}")
        print(f"Running {command}...", file=sys.stderr)
        # Do not capture suite output: backend and browser targets can be long
        # and noisy.  Stream it directly so this option has bounded memory.
        completed = subprocess.run(["make", "--no-print-directory", target], cwd=root, check=False)
        outcomes.append({"command": command, "returncode": completed.returncode})
        if completed.returncode:
            raise ResolverError(f"{command} failed with status {completed.returncode}")
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("BASE", "HEAD~1"), help="committed diff BASE (default: BASE or HEAD~1)")
    parser.add_argument("--head", default=os.environ.get("HEAD", "HEAD"), help="committed diff HEAD (default: HEAD or HEAD)")
    parser.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--filters", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of human text")
    parser.add_argument("--run", action="store_true", help="run the supported local make targets after resolving")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo.resolve()
    filters_path = (args.filters or (root / ".github" / "path-filters.yml")).resolve()
    try:
        sources = collect_paths(root, args.base, args.head)
        filters = _load_filters(filters_path)
        result = resolve(filters, sources)
        if args.run:
            if args.json:
                raise ResolverError("--run cannot be combined with --json because target output is streamed")
            if result["unknown_paths"]:
                raise ResolverError(
                    "refusing --run for unmatched paths; review which CI suite covers them first: "
                    + ", ".join(result["unknown_paths"])
                )
            result["runs"] = _run_commands(root, result["commands"])
        else:
            result["runs"] = []
    except ResolverError as exc:
        print(f"affected-check: ERROR: {exc}", file=sys.stderr)
        return 2

    result["base"] = args.base
    result["head"] = args.head
    result["filters"] = str(filters_path.relative_to(root) if filters_path.is_relative_to(root) else filters_path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(_human(result, base=args.base, head=args.head))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
