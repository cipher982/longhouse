#!/usr/bin/env python3
"""Every check in scripts/qa is reached by something, or says why it is not.

"A check whose result nothing reads is not a check" is a rule this repo states
and did not enforce. The first attempt to measure it asked whether a basename
appeared in the Makefile, the pre-commit config or a workflow, and reported 24 of
69 unread. Most of those 24 are invoked by another script, a test or a runbook,
and the artifact doing the counting listed the very names it was counting, so it
matched itself. A negative search proves only the surfaces it searched.

This computes reachability instead, from the places work actually starts. What is
left over is a file nothing reaches -- which is either a defect or a tool meant
for a human, and the two are indistinguishable from the outside. So the file
itself says which: a hand-run tool carries a `Run by hand:` line explaining why
automation would be wrong.

There is no separate manifest on purpose. A list of exempt filenames is one more
thing to drift; the declaration lives in the file it describes, and a new orphan
that says nothing fails.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
QA_DIR = ROOT / "scripts" / "qa"
DECLARATION = "Run by hand"
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2", ".mp4", ".pdf")


def _texts() -> dict[str, str]:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    out: dict[str, str] = {}
    for rel in tracked:
        path = ROOT / rel
        if path.suffix in SKIP_SUFFIXES:
            continue
        # Generated inventories list these filenames as data. Counting them as
        # references is how the first version of this measurement fooled itself.
        if rel.startswith("docs/generated/"):
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    return out


def _reachable(texts: dict[str, str]) -> set[str]:
    roots = {
        rel
        for rel in texts
        if rel in ("Makefile", ".pre-commit-config.yaml")
        or rel.startswith(".github/workflows/")
        or rel.startswith("docs/")
    }
    reachable = set(roots)
    frontier = list(roots)
    while frontier:
        body = texts.get(frontier.pop(), "")
        for rel in texts:
            if rel in reachable:
                continue
            name = pathlib.Path(rel).name
            module = pathlib.Path(rel).stem.replace("-", "_")
            if name in body or re.search(rf"\b{re.escape(module)}\b", body):
                reachable.add(rel)
                frontier.append(rel)
    return reachable


def _declares_hand_run(path: pathlib.Path) -> bool:
    """A declaration is a line that *is* the declaration, not a mention of one.

    Substring matching let this very file exempt itself, because its docstring
    explains what the marker looks like. A check that passes itself by describing
    the exemption is worth less than no check.
    """
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.lstrip().lstrip("#").lstrip().startswith(DECLARATION):
            return True
    return False


def main() -> int:
    texts = _texts()
    reachable = _reachable(texts)
    scripts = sorted(p for p in QA_DIR.glob("*") if p.is_file() and not p.name.startswith("."))

    # Reachability by name, not by membership: a check added but not yet
    # committed is absent from `git ls-files` and would otherwise be reported as
    # unreached while the Makefile is already invoking it.
    reachable_text = "\n".join(texts[rel] for rel in reachable if rel in texts)

    undeclared: list[str] = []
    hand_run: list[str] = []
    for path in scripts:
        rel = str(path.relative_to(ROOT))
        if rel in reachable or path.name in reachable_text:
            continue
        if _declares_hand_run(path):
            hand_run.append(rel)
        else:
            undeclared.append(rel)

    if undeclared:
        print("These checks are reached by nothing and do not say why:\n")
        for rel in undeclared:
            print(f"  - {rel}")
        print(
            f"\nWire it into the Makefile, a hook, a workflow or a runbook, or add a "
            f'"{DECLARATION}:" line saying why automation would be wrong. A check '
            f"nothing reads is not a check."
        )
        return 1

    print(
        f"scripts/qa: {len(scripts)} checks, {len(scripts) - len(hand_run)} reached by "
        f"an entrypoint, {len(hand_run)} declared hand-run."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
