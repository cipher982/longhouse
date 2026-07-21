#!/usr/bin/env python3
"""Reject duplicate prose definitions of Longhouse session modes."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_DOC = REPO_ROOT / "ARCHITECTURE.md"
HISTORICAL_SPECS = Path("docs") / "specs"
TARGET_TERMS = "Shadow|Helm|Console|Managed session|Unmanaged session"
DEFINITION_RE = re.compile(
    rf"^\s*[-*+]\s+\*\*(?:{TARGET_TERMS})\*\*\s*[-:\u2013\u2014]\s+\S"
)


def markdown_files() -> list[Path]:
    """Return repository Markdown files, excluding historical design notes."""
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.md")
        if HISTORICAL_SPECS not in path.relative_to(REPO_ROOT).parents
    )


def canonical_lines(path: Path, line_count: int) -> set[int]:
    """Return lines belonging to ARCHITECTURE.md's Session modes section."""
    if path != CANONICAL_DOC:
        return set()

    lines = path.read_text(encoding="utf-8").splitlines()
    section_start = next(
        (index for index, line in enumerate(lines) if line == "## Session modes"),
        None,
    )
    if section_start is None:
        raise SystemExit("ARCHITECTURE.md is missing the canonical Session modes section")

    section_end = next(
        (
            index
            for index in range(section_start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    return set(range(section_start + 1, section_end + 1)) & set(range(1, line_count + 1))


def find_duplicate_definitions() -> list[tuple[Path, int, str]]:
    violations = []
    for path in markdown_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        allowed_lines = canonical_lines(path, len(lines))
        for line_number, line in enumerate(lines, start=1):
            if line_number in allowed_lines:
                continue
            if DEFINITION_RE.match(line):
                violations.append((path.relative_to(REPO_ROOT), line_number, line.strip()))
    return violations


def main() -> int:
    violations = find_duplicate_definitions()
    if violations:
        print("Duplicate session-mode definitions found; link to ARCHITECTURE.md#session-modes instead:", file=sys.stderr)
        for path, line_number, line in violations:
            print(f"  {path}:{line_number}: {line}", file=sys.stderr)
        return 1

    print("Session-mode definition check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
