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


def _candidate_files() -> dict[str, str]:
    """Files that could plausibly cause a check to run.

    A Rust source file does not invoke a QA script; a Makefile, a workflow, a
    runbook, another script or a test does. Reading all 2,356 tracked files and
    running the closure over the whole graph took long enough that two reviewers
    called it a check nobody would wait for.
    """
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "Makefile",
            ".pre-commit-config.yaml",
            ".github",
            "docs",
            "scripts",
            "*.md",
            "*/scripts/*",
            "web/scripts",
            "server/tests_lite",
            "scripts/tests",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    out: dict[str, str] = {}
    for rel in dict.fromkeys(tracked):
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


def _reached(scripts: list[pathlib.Path], texts: dict[str, str]) -> set[str]:
    """One scan pass, then a fixed point over the scripts alone.

    The first version re-scanned every candidate file once per frontier item,
    which is where the minutes went. Mentions are collected in a single pass and
    the closure runs over ~70 nodes instead.
    """
    # One alternation over every script name, scanned once per file. Compiling
    # seventy patterns and running each against every file was the remaining
    # cost after the candidate set was narrowed.
    by_token: dict[str, str] = {}
    for path in scripts:
        rel = str(path.relative_to(ROOT))
        by_token[path.name] = rel
        by_token.setdefault(path.stem.replace("-", "_"), rel)
    combined = re.compile(
        "|".join(re.escape(token) for token in sorted(by_token, key=len, reverse=True))
    )

    mentions: dict[str, set[str]] = {}
    for rel, body in texts.items():
        mentions[rel] = {by_token[m.group(0)] for m in combined.finditer(body)}

    def is_root(rel: str) -> bool:
        """Where work starts, or where a human is told to start it.

        Tooling scripts outside scripts/qa count: `web/scripts/run-vitest.mjs`
        invokes a checker here, and is itself invoked by package.json. Treating
        only Makefiles and docs as roots reported that checker as unread.
        """
        if rel in ("Makefile", ".pre-commit-config.yaml"):
            return True
        if rel.startswith((".github/workflows/", "docs/")) or rel.endswith(".md"):
            return True
        if rel.startswith(("server/tests_lite/", "scripts/tests/")):
            return True
        return "/scripts/" in f"/{rel}" and not rel.startswith("scripts/qa/")

    reached = {s for rel in texts if is_root(rel) for s in mentions[rel]}
    changed = True
    while changed:
        changed = False
        for rel, referenced in mentions.items():
            if rel in reached and not referenced <= reached:
                reached |= referenced
                changed = True
    return reached


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
    texts = _candidate_files()
    scripts = sorted(p for p in QA_DIR.glob("*") if p.is_file() and not p.name.startswith("."))
    reached = _reached(scripts, texts)

    undeclared: list[str] = []
    hand_run: list[str] = []
    stale: list[str] = []
    for path in scripts:
        rel = str(path.relative_to(ROOT))
        if rel in reached:
            # A declaration on a file something already runs is a false
            # statement, and the kind that rots quietly: it was true when
            # written and nothing revisits it.
            if _declares_hand_run(path):
                stale.append(rel)
            continue
        if _declares_hand_run(path):
            hand_run.append(rel)
        else:
            undeclared.append(rel)

    if stale:
        print("These say they are hand-run, but something already runs them:\n")
        for rel in stale:
            print(f"  - {rel}")
        print('\nRemove the "Run by hand:" line; it is no longer true.')
        return 1

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
