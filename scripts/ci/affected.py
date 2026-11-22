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
    "ui_sweep": FilterPlan(
        ("ubuntu-24.04-arm",),
        ("make test-ui-sweep ARGS=--viewports=desktop", "make test-ui-sweep ARGS=--viewports=mobile"),
        "A push sweeps the desktop and mobile sizes; the nightly sweep adds wide.",
    ),
    "runtime_image": FilterPlan(
        ("cube-maint",),
        (),
        "Runtime-image assembly is CI-owned; no local deployment is run by this resolver.",
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
_UNSUPPORTED_PATTERN = re.compile(r"(?:\{|\}|\(|\)|\[|\]|\\)")


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


def collect_paths(root: Path, base: str, head: str, include_dirty: bool = False) -> dict[str, list[str]]:
    """The paths a push would change: ``base...head``, i.e. head's commits since the merge base.

    Only committed work. Another session's staged, unstaged and untracked files in a shared checkout are not part of
    what is being pushed (2026-11-15: a gate that read them selected lanes for work nobody was shipping). Pass
    ``include_dirty`` to add them for a local what-if.
    """

    verified_base = _verify_revision(root, base, "BASE")
    verified_head = _verify_revision(root, head, "HEAD")
    committed = _nul_paths(
        _git(root, ["diff", "--name-only", "--no-renames", "-z", f"{verified_base}...{verified_head}"])
    )
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    if include_dirty:
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
            # A leading `!` excludes (dorny predicate-quantifier some-with-excludes).
            body = pattern[1:] if isinstance(pattern, str) and pattern.startswith("!") else pattern
            if (
                not isinstance(pattern, str)
                or not body
                or body.startswith(("/", "!"))
                or _UNSUPPORTED_PATTERN.search(body)
                or body.count("[") != body.count("]")
            ):
                raise ResolverError(f"path filter {category!r} contains unsupported pattern {pattern!r}")
            if "\x00" in pattern:
                raise ResolverError(f"path filter {category!r} contains NUL in pattern")
            _compile_pattern(body)
            normalized.append(pattern)
        if all(pattern.startswith("!") for pattern in normalized):
            raise ResolverError(f"path filter {category!r} has only exclusions")
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


def _in_category(path: str, patterns: tuple[str, ...]) -> bool:
    """A path is in a category when some pattern matches and no `!` pattern does."""

    excluded = any(_matches(path, pattern[1:]) for pattern in patterns if pattern.startswith("!"))
    included = any(_matches(path, pattern) for pattern in patterns if not pattern.startswith("!"))
    return included and not excluded


def resolve(filters: dict[str, tuple[str, ...]], paths_by_source: dict[str, list[str]]) -> dict[str, Any]:
    """Resolve files to every matching category and its execution advice."""

    all_paths = sorted({path for paths in paths_by_source.values() for path in paths})
    matches: dict[str, list[str]] = {}
    category_paths: dict[str, list[str]] = {category: [] for category in filters}
    unknown_paths: list[str] = []
    for path in all_paths:
        categories = [category for category, patterns in filters.items() if _in_category(path, patterns)]
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



# ---------------------------------------------------------------------------------------------------------------------
# The pre-push gate: the lanes CI would run for these paths, runnable before the push.
# ---------------------------------------------------------------------------------------------------------------------

TERMINAL_TRUTH_WORKFLOW = ".github/workflows/terminal-truth.yml"


@dataclass(frozen=True)
class Lane:
    name: str
    command: str
    where: str  # "local" (cheap, no build) or "remote" (the ci VM through scripts/ops/remote.sh)
    why: str


def terminal_truth_paths(root: Path) -> tuple[str, ...]:
    """The push filter of the Terminal Truth workflow, read from it so the two cannot drift."""

    try:
        doc = yaml.safe_load((root / TERMINAL_TRUTH_WORKFLOW).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ResolverError(f"cannot read {TERMINAL_TRUTH_WORKFLOW}: {exc}") from exc
    triggers = doc.get(True) or doc.get("on") or {}
    paths = (triggers.get("push") or {}).get("paths") or []
    if not paths:
        raise ResolverError(f"{TERMINAL_TRUTH_WORKFLOW} has no push paths filter")
    return tuple(str(p) for p in paths)


def gate_plan(root: Path, result: dict[str, Any], *, backend_select: Any | None = None) -> dict[str, Any]:
    """Lanes for ``result`` (from :func:`resolve`) that mirror ``.github/workflows/contract-first-ci.yml``.

    ``make validate`` and the provider contract assertions run on every CI push, so they run on every gate. The rest
    follow CI's own conditions. Lanes CI runs that the gate cannot (iOS on wisp, browsers, credentialed model smoke,
    the nightly UI sweep) are listed in ``ci_only`` so a green gate never reads as "CI will pass".
    """

    files: list[str] = result["files"]
    cats = set(result["categories"])
    lanes: list[Lane] = []
    ci_only: list[str] = []
    notes: list[str] = []
    if not files:
        return {"lanes": [], "ci_only": [], "notes": ["no committed changes against the base"], "backend": None}

    if "engine" in cats and any(f.startswith("engine/") for f in files):
        lanes.append(Lane("rustfmt", "cargo fmt --manifest-path engine/Cargo.toml -- --check", "local",
                          "CI Engine rustfmt gate (engine/**)"))
    lanes.append(Lane("validate", "make validate", "remote", "CI Validation runs on every push"))
    lanes.append(Lane("provider-contract", "make test-provider-contract", "remote",
                      "CI provider-contract-tests runs on every push"))
    if cats & {"backend", "engine", "schemas", "scripts", "ci_plumbing"}:
        lanes.append(Lane("managed-launch", "make test-managed-launch-lifecycle", "remote",
                          "CI Managed launch lifecycle (backend, engine, schemas, scripts or CI plumbing changed)"))
    if cats & {"backend", "scripts", "schemas", "engine"}:
        # CI runs the shards for backend|scripts; schemas and engine feed generated files the tests read.
        if backend_select is None:
            from importlib import util as _u

            spec = _u.spec_from_file_location("backend_select", root / "scripts" / "ci" / "backend_select.py")
            assert spec and spec.loader
            backend_select = _u.module_from_spec(spec)
            sys.modules.setdefault("backend_select", backend_select)
            spec.loader.exec_module(backend_select)
        selection = backend_select.select(root, files)
        if selection.full:
            lanes.append(Lane("backend-full", "make test", "remote", f"full backend suite: {selection.reason}"))
        elif selection.tests:
            targets = " ".join(selection.tests)
            lanes.append(Lane("backend-selected", f'make test TEST="{targets}"', "remote", selection.reason))
        else:
            notes.append(f"backend tests: none reach the change ({selection.reason})")
        backend = selection.as_dict()
    else:
        backend = None
    if cats & {"engine", "backend"}:
        lanes.append(Lane("engine", "make test-engine", "remote", "CI Engine tests (engine/** or server/** changed)"))
        lanes.append(Lane("shipper-e2e", "make test-shipper-e2e", "remote",
                          "CI Shipper pipeline E2E (fixture -> engine -> API -> DB)"))
    if cats & {"frontend", "runner"}:
        lanes.append(Lane("frontend", "make test-frontend-runner", "remote", "CI Frontend + runner tests"))
    if "packaging" in cats:
        lanes.append(Lane("wheel", "make test-wheel-package", "remote", "CI wheel packaging check"))
    tt_paths = terminal_truth_paths(root)
    tt_hits = [f for f in files if any(_matches(f, pattern) for pattern in tt_paths)]
    if tt_hits:
        lanes.append(Lane("terminal-truth-selfcheck",
                          "python3 scripts/qa/test-isolation.py --target test-terminal-truth-selfcheck --command make --no-print-directory terminal-truth-selfcheck",
                          "remote",
                          f"Terminal Truth paths changed ({tt_hits[0]}{' ...' if len(tt_hits) > 1 else ''}): harness selfcheck, no provider"))
        ci_only.append("Terminal Truth real-provider cells (the stock provider CLIs never run on the laptop or the ci VM guest): "
                       "the post-push run is the verdict and a red main is fixed forward by the author")
    for cat, text in (
        ("ios", "iOS tests on wisp"),
        ("e2e", "Core E2E (Playwright)"),
        ("models", "credentialed model smoke"),
        ("ui_sweep", "UI popover sweep"),
    ):
        if cat in cats:
            ci_only.append(text)
    ci_only.append("engine-compat, OSS onboarding, transcript-stability replays (CI small checks)")
    return {
        "lanes": [lane.__dict__ for lane in lanes],
        "ci_only": ci_only,
        "notes": notes,
        "backend": backend,
        "terminal_truth_paths_hit": tt_hits,
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


def _human_gate(gate: dict[str, Any]) -> str:
    lines = ["Pre-push gate lanes:"]
    for lane in gate["lanes"]:
        lines.append(f"  [{lane['where']}] {lane['name']}: {lane['command']}  ({lane['why']})")
    if not gate["lanes"]:
        lines.append("  (none)")
    for note in gate["notes"]:
        lines.append(f"Note: {note}")
    for item in gate["ci_only"]:
        lines.append(f"CI only, not run by the gate: {item}")
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
    parser.add_argument("--base", default=os.environ.get("BASE", "origin/main"), help="push base (default: BASE or origin/main); the diff is base...head")
    parser.add_argument("--head", default=os.environ.get("HEAD", "HEAD"), help="committed diff HEAD (default: HEAD or HEAD)")
    parser.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--filters", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of human text")
    parser.add_argument("--run", action="store_true", help="run the supported local make targets after resolving")
    parser.add_argument("--include-dirty", action="store_true", help="also count staged, unstaged and untracked files (local what-if)")
    parser.add_argument("--gate", action="store_true", help="print the pre-push gate plan (the CI lanes for these paths)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo.resolve()
    filters_path = (args.filters or (root / ".github" / "path-filters.yml")).resolve()
    try:
        sources = collect_paths(root, args.base, args.head, include_dirty=args.include_dirty)
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
        if args.gate:
            result["gate"] = gate_plan(root, result)
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
        if args.gate:
            print(_human_gate(result["gate"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
