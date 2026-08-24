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
import pathlib
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


def _unread_checks() -> dict:
    """A check whose result nothing reads is not a check.

    The first version of this asked "is the basename mentioned in the Makefile,
    pre-commit config or a workflow?" and reported 24 of 69 unread. Widening the
    search showed most of those 24 are invoked by another script, a test, or a
    runbook -- and that this artifact, which lists the filenames, was matching
    itself. A negative search proves only the surfaces it searched.

    So this computes reachability instead. Roots are the places work actually
    starts: the Makefile, the pre-commit config, CI workflows, and docs (a tool
    a runbook tells a human to run is reached by a human). A file referenced by
    a reachable file is reachable. What is left over is referenced by nothing
    that anything starts from -- including scripts reachable only from another
    orphan.
    """
    qa_dir = ROOT / "scripts" / "qa"
    scripts = sorted(p for p in qa_dir.glob("*") if p.is_file() and not p.name.startswith("."))

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    texts: dict[str, str] = {}
    for rel in tracked:
        # This artifact lists the very names it is measuring; counting it as a
        # reference would make every orphan look reached.
        if rel == str(OUTPUT.relative_to(ROOT)):
            continue
        path = ROOT / rel
        if path.suffix in (".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".mp4", ".pdf"):
            continue
        try:
            texts[rel] = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    roots = {
        rel
        for rel in texts
        if rel == "Makefile"
        or rel == ".pre-commit-config.yaml"
        or rel.startswith(".github/workflows/")
        or rel.startswith("docs/")
    }

    def references(text: str, name: str) -> bool:
        module = pathlib.Path(name).stem.replace("-", "_")
        return name in text or re.search(rf"\b{re.escape(module)}\b", text) is not None

    reachable: set[str] = set(roots)
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        body = texts.get(current, "")
        for rel in texts:
            if rel in reachable:
                continue
            if references(body, pathlib.Path(rel).name):
                reachable.add(rel)
                frontier.append(rel)

    unreachable = sorted(
        str(p.relative_to(ROOT)) for p in scripts if str(p.relative_to(ROOT)) not in reachable
    )
    return {
        "rule": (
            "Reachability, not mention. Roots are Makefile, .pre-commit-config.yaml, "
            ".github/workflows/*, and docs/*; an edge exists where one file's text "
            "contains another's basename or underscored module name. This artifact is "
            "excluded as a referencer because it lists the names it measures. Name "
            "matching is a heuristic and will over-report reachability, so this is a "
            "floor: everything listed is unreachable, but not everything unreachable "
            "is necessarily listed."
        ),
        "checks_total": len(scripts),
        "unreachable_count": len(unreachable),
        "unreachable": unreachable,
    }


def build() -> dict:
    sources = _tracked_sources()
    providers = _providers()
    return {
        "purpose": (
            "Two measurements, both computed from the tree. Launch-identity coverage "
            "is deliberately absent: scripts/qa/check-managed-identity-sites.py owns "
            "that property, and the weaker copy that used to live here counted the "
            "overlay's own unit tests as launch sites -- it would have reported five "
            "of six providers covered with every real launcher deleted. Judgment about "
            "which axis to close next lives in the spec, where a reader can see it is "
            "a person's opinion rather than a computed fact."
        ),
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
        "member_collections": {
            "managed_providers": _collection_literals(providers, sources),
        },
        "unread_checks": _unread_checks(),
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
