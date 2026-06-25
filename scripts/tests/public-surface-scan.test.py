#!/usr/bin/env python3
"""Guard public-facing docs against private/local implementation leakage."""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_ROOTS = (
    Path("README.md"),
    Path("VISION.md"),
    Path(".env.example"),
    Path("docs/specs"),
    Path("docs/runbooks"),
    Path("docs/tasks"),
    Path(".agents/skills"),
)

TEXT_SUFFIXES = {".md", ".txt", ".example"}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


RULES = (
    Rule("personal owner email", re.compile(r"\bdavid010@gmail\.com\b", re.IGNORECASE)),
    Rule("personal tenant hostname", re.compile(r"\bdavid010\.longhouse\.ai\b", re.IGNORECASE)),
    Rule("personal tenant slug", re.compile(r"(?<![A-Za-z0-9_.@-])david010(?![A-Za-z0-9_.@-])", re.IGNORECASE)),
    Rule("personal macOS path", re.compile(r"/Users/davidrose\b")),
    Rule("personal NAS path", re.compile(r"/volume1/homes/drose\b")),
    Rule("personal hosted container", re.compile(r"\blonghouse-david010\b", re.IGNORECASE)),
    Rule("personal SSH host alias", re.compile(r"\bssh\s+zerg\b", re.IGNORECASE)),
    Rule(
        "personal tenant make variable",
        re.compile(r"\b(?:SUBDOMAIN|QA_INSTANCE_SUBDOMAIN)=david010\b", re.IGNORECASE),
    ),
    Rule(
        "internal agent review process",
        re.compile(r"\bHatch\b", re.IGNORECASE),
    ),
    Rule(
        "personal maintainer approval process",
        re.compile(r"\bDavid (?:dogfooded|approval|explicitly|approved)\b|David-approved", re.IGNORECASE),
    ),
    Rule(
        "internal Codex review process",
        re.compile(r"\bCodex reviewed\b|codex-confirmed", re.IGNORECASE),
    ),
)


def iter_scan_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in SCAN_ROOTS:
        path = root / relative
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        for candidate in sorted(path.rglob("*")):
            if candidate.is_file() and candidate.suffix in TEXT_SUFFIXES:
                files.append(candidate)
    return sorted(files)


def scan(root: Path) -> list[str]:
    failures: list[str] = []
    for path in iter_scan_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{path.relative_to(root)}: cannot decode as UTF-8: {exc}")
            continue

        relative = path.relative_to(root)
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule in RULES:
                if rule.pattern.search(line):
                    snippet = line.strip()
                    if len(snippet) > 140:
                        snippet = snippet[:137] + "..."
                    failures.append(f"{relative}:{line_number}: {rule.name}: {snippet}")
    return failures


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_self_tests() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write(root / "README.md", "Install Longhouse from get.longhouse.ai.\n")
        assert scan(root) == []

        _write(root / "docs/specs/private.md", "Owner: david010@gmail.com\n")
        failures = scan(root)
        assert len(failures) == 1, failures
        assert "personal owner email" in failures[0], failures

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write(root / "docs/runbooks/reclaim.md", "ssh zerg 'docker exec longhouse-david010 true'\n")
        failures = scan(root)
        assert len(failures) == 2, failures
        assert any("personal SSH host alias" in failure for failure in failures), failures
        assert any("personal hosted container" in failure for failure in failures), failures

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write(root / "docs/specs/review.md", "## Hatch Opus Review\n")
        failures = scan(root)
        assert len(failures) == 1, failures
        assert "internal agent review process" in failures[0], failures

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write(root / "docs/runbooks/reclaim.md", "David approval before production reclaim.\n")
        failures = scan(root)
        assert len(failures) == 1, failures
        assert "personal maintainer approval process" in failures[0], failures

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write(root / "docs/specs/review.md", "Codex reviewed the plan.\n")
        failures = scan(root)
        assert len(failures) == 1, failures
        assert "internal Codex review process" in failures[0], failures


def main() -> int:
    run_self_tests()
    failures = scan(REPO_ROOT)
    if failures:
        print("Public-surface scan failed. Replace private/local details with public examples:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("Public-surface scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
