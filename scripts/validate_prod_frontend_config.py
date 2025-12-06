#!/usr/bin/env python3
"""Guard production build scripts: no localhost API override in prod branch.

Checks that in `frontend/build-only.sh` and `frontend/build-debug.sh`, the
conditional branch for BUILD_ENV==production does NOT contain an assignment to
`window.API_BASE_URL` or a hardcoded `localhost`.

This is a static heuristic (no shell execution). It searches for the first
`if [[ "${BUILD_ENV}" == "production" ]]` and inspects the lines until the
matching `else` or `fi` at the same nesting depth.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


CHECK_FILES = [
    Path("frontend/build-only.sh"),
    Path("frontend/build-debug.sh"),
]


def extract_prod_branch(lines: list[str]) -> list[str] | None:
    prod_if_re = re.compile(r"^\s*if\s*\[\[\s*\"\$\{?BUILD_ENV\}?\"\s*==\s*\"production\"\s*\]\]\s*;?\s*then\s*$")
    depth = 0
    inside = False
    block: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if prod_if_re.match(stripped) and not inside and depth == 0:
            inside = True
            depth = 1
            continue

        # Track nesting depth conservatively
        if inside:
            # End of current block when `else` or `fi` at depth 1
            if re.match(r"^\s*else\s*$", stripped) and depth == 1:
                return block
            if re.match(r"^\s*fi\s*$", stripped):
                depth -= 1
                if depth == 0:
                    return block
                continue
            if re.match(r"^\s*if\b", stripped):
                depth += 1
            block.append(stripped)

    return block if inside else None


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        errors.append(f"Missing file: {path}")
        return errors
    lines = path.read_text(encoding="utf-8").splitlines()
    prod_block = extract_prod_branch(lines)
    if prod_block is None:
        errors.append(f"{path}: could not locate BUILD_ENV=='production' branch")
        return errors

    joined = "\n".join(prod_block)
    if "window.API_BASE_URL" in joined:
        errors.append(
            f"{path}: production branch must not assign window.API_BASE_URL (found in prod block)"
        )
    if re.search(r"localhost(:\d+)?", joined):
        errors.append(
            f"{path}: production branch must not reference localhost (found in prod block)"
        )
    return errors


def main() -> int:
    failures: list[str] = []
    for file in CHECK_FILES:
        failures.extend(validate_file(file))
    if failures:
        print("✖ Production config guard failed:")
        for f in failures:
            print(f"  - {f}")
        print("\nFix: ensure production branches do not set window.API_BASE_URL or reference localhost.")
        return 1
    print("✅ Production config guard passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
