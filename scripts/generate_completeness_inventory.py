#!/usr/bin/env python3
"""Measure where the omission bug class can still hide.

A drift check compares two artifacts that both exist, so it cannot see a member
that was never in either. Ten verified instances of that blind spot are recorded
in provider-surface-coherence.md; an eleventh (OpenCode Helm never tagging its
launch) was found and fixed on 2026-08-24.

This measures the surfaces where the same shape could recur, so the next axis is
chosen from numbers rather than from an argument. It asserts nothing about
whether a finding is a defect: a hand-written member collection is often
deliberate policy. The point is that the claim becomes checkable.

Counting rules are stated in the output, because a count without its rule is not
evidence. In particular this is NOT docs/generated/provider_census.json, which
counts files containing two or more provider-name literals *anywhere*. This
counts collection literals -- a list, array, tuple or set naming two or more
members of a derived registry -- which is the shape that silently omits the next
member.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "generated" / "completeness.json"
SCHEMA = ROOT / "schemas" / "managed_providers.yml"

SOURCE_GLOBS = ("*.py", "*.ts", "*.tsx", "*.rs", "*.swift")
EXCLUDED_PATH_PARTS = ("generated", "node_modules", "target")

COLLECTION_RULE = (
    "A bracketed literal (list, array, tuple or set) containing quoted string "
    "literals for two or more distinct members of the registry, on one line, in "
    "a git-tracked .py/.ts/.tsx/.rs/.swift file, excluding paths containing "
    "'generated', 'node_modules' or 'target'."
)


def _tracked_sources() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", *SOURCE_GLOBS], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    paths = []
    for rel in out:
        if any(part in rel.split("/") for part in EXCLUDED_PATH_PARTS):
            continue
        paths.append(ROOT / rel)
    return paths


def _providers() -> list[str]:
    payload = yaml.safe_load(SCHEMA.read_text(encoding="utf-8"))
    return sorted(str(row["provider"]) for row in payload["providers"])


def _collection_literals(members: list[str], sources: list[Path]) -> dict:
    alternation = "|".join(re.escape(m) for m in members)
    pattern = re.compile(
        r"""[\[\(\{][^\[\(\{\]\)\}]*?(?:['"](?:%s)['"][^\[\(\{\]\)\}]*?){2,}[\]\)\}]""" % alternation
    )
    per_file: dict[str, int] = {}
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = pattern.findall(text)
        if hits:
            per_file[str(path.relative_to(ROOT))] = len(hits)
    in_tests = sum(count for name, count in per_file.items() if "test" in name.lower())
    return {
        "rule": COLLECTION_RULE,
        "files": len(per_file),
        "literals": sum(per_file.values()),
        "literals_in_tests": in_tests,
        "literals_in_source": sum(per_file.values()) - in_tests,
        "top_files": dict(sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))[:15]),
    }


def _launch_identity(sources: list[Path], providers: list[str]) -> dict:
    routed: dict[str, list[str]] = {}
    for provider in providers:
        variant = "".join(part.capitalize() for part in provider.split("_"))
        pattern = re.compile(r"ManagedIdentity::new\(\s*ManagedProvider::%s\b" % re.escape(variant))
        sites = sorted(
            str(p.relative_to(ROOT))
            for p in sources
            if p.suffix == ".rs" and pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
        )
        if sites:
            routed[provider] = sites
    # A launch source hand-writing these keys is the defect. A test harness doing
    # it is simulating what a launcher produced, which is the point of the test --
    # so the two are counted separately rather than summed into one alarming
    # number.
    in_launch_sources: list[str] = []
    in_test_harnesses: list[str] = []
    guarded = ("LONGHOUSE_MANAGED_SESSION_ID", "LONGHOUSE_MANAGED_PROVIDER")
    for path in sources:
        if path.suffix != ".rs" or path.name.startswith("managed_identity"):
            continue
        rel = str(path.relative_to(ROOT))
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            if line.lstrip().startswith("//") or ".env(" not in line:
                continue
            if any(f'"{key}"' in line for key in guarded):
                bucket = in_test_harnesses if "/tests/" in rel else in_launch_sources
                bucket.append(f"{rel}:{number}")
    return {
        "registry": "schemas/managed_providers.yml",
        "anchor": "load-bearing: the factory, census, contract digest and generated Rust enum all derive from it",
        "check": "scripts/qa/check-managed-identity-sites.py",
        "providers_routed": routed,
        "providers_unrouted": [p for p in providers if p not in routed],
        "hand_written_in_launch_sources": in_launch_sources,
        "hand_written_in_test_harnesses": in_test_harnesses,
    }


def _unread_checks() -> dict:
    """A check whose result nothing reads is not a check."""
    scripts = sorted(
        p for p in (ROOT / "scripts" / "qa").glob("*") if p.is_file() and not p.name.startswith(".")
    )
    readers: list[str] = []
    for candidate in [ROOT / "Makefile", ROOT / ".pre-commit-config.yaml"]:
        if candidate.exists():
            readers.append(candidate.read_text(encoding="utf-8", errors="ignore"))
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        readers.append(path.read_text(encoding="utf-8", errors="ignore"))
    blob = "\n".join(readers)
    unreferenced = [p.name for p in scripts if p.name not in blob]
    return {
        "rule": "A file in scripts/qa/ whose basename appears in no Makefile, pre-commit config or workflow.",
        "checks_total": len(scripts),
        "unreferenced": unreferenced,
    }


def _candidate_axes() -> list[dict]:
    """Axes named as out of scope for the launch-identity work, measured only.

    Recorded so a later epic is scoped from evidence. `derived_registry: false`
    means the members exist only as prose or as repeated literals -- which is
    where this bug class lives.
    """
    return [
        {
            "axis": "managed launch identity",
            "derived_registry": True,
            "completeness_check": True,
            "status": "closed by scripts/qa/check-managed-identity-sites.py",
        },
        {
            "axis": "session modes (Shadow / Helm / Console)",
            "derived_registry": True,
            "completeness_check": True,
            "status": "session-mode-definitions pre-commit hook already checks the vocabulary",
        },
        {
            "axis": "native device entrypoints",
            "derived_registry": True,
            "completeness_check": True,
            "status": "scripts/qa/check-native-device-entrypoints.py, run via make validate",
        },
        {
            "axis": "data tiers x datasets",
            "derived_registry": False,
            "completeness_check": False,
            "status": "AGENTS.md states every dataset belongs to exactly one tier; no registry of datasets exists, so totality is unenforceable as written",
        },
        {
            "axis": "retention roots x reachability",
            "derived_registry": False,
            "completeness_check": False,
            "status": "AGENTS.md states retention must reach every root; roots are not enumerated anywhere",
        },
        {
            "axis": "/api/* routes on api_app",
            "derived_registry": False,
            "completeness_check": False,
            "status": "stated in AGENTS.md, enforced by review only; mechanically greppable",
        },
    ]


def build() -> dict:
    sources = _tracked_sources()
    providers = _providers()
    return {
        "purpose": "Where the omission bug class can still hide. Measured, not asserted.",
        "not_the_provider_census": (
            "docs/generated/provider_census.json counts files with >=2 provider literals anywhere. "
            "This counts collection literals. Different rules, different numbers; neither target is zero."
        ),
        # The scan *rule* is recorded; the raw file count deliberately is not.
        # It moves whenever anyone adds a source file anywhere, which would make
        # this inventory go stale on unrelated commits -- and a check that fails
        # for unrelated reasons is one people learn to ignore. The counts below
        # move only when the thing being measured moves.
        "scan_rule": (
            f"git-tracked {', '.join(SOURCE_GLOBS)} excluding any path component in "
            f"{', '.join(EXCLUDED_PATH_PARTS)}"
        ),
        "launch_identity": _launch_identity(sources, providers),
        "member_collections": {
            "managed_providers": _collection_literals(providers, sources),
        },
        "unread_checks": _unread_checks(),
        "candidate_axes": _candidate_axes(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the inventory is stale")
    args = parser.parse_args()
    rendered = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT} is stale. Run `make generate-completeness`.", file=sys.stderr)
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is current.")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)} (scanned {len(_tracked_sources())} sources)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
