#!/usr/bin/env python3
"""Fail when the ARCHITECTURE.md code map cites a path that does not exist.

Every backticked token in the "## Code map" section that contains a "/" is
read as a repository path. A token with "*" must match at least one file.
Tokens starting with "/" or "@/" are routes and import aliases, not paths.
"""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "ARCHITECTURE.md"
SECTION = "## Code map"
TOKEN = re.compile(r"`([^`\s]+)`")


def code_map(text: str) -> str:
    start = text.find(f"\n{SECTION}\n")
    if start < 0:
        raise SystemExit(f"{DOC.name}: no '{SECTION}' section")
    end = text.find("\n## ", start + len(SECTION) + 2)
    return text[start : end if end >= 0 else len(text)]


def cited_paths(section: str) -> list[str]:
    paths = []
    for token in TOKEN.findall(section):
        if "/" not in token or token.startswith(("/", "@/", "http")):
            continue
        paths.append(token)
    return paths


def exists(path: str) -> bool:
    if "*" in path:
        return bool(glob.glob(str(REPO_ROOT / path)))
    return (REPO_ROOT / path).exists()


def main() -> int:
    paths = cited_paths(code_map(DOC.read_text(encoding="utf-8")))
    missing = sorted({path for path in paths if not exists(path)})
    for path in missing:
        print(f"ARCHITECTURE.md code map cites a missing path: {path}", file=sys.stderr)
    if missing:
        return 1
    print(f"code map: {len(set(paths))} paths resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
