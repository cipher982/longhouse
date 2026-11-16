#!/usr/bin/env python3
"""scripts/ci/prepush_gate.py: which pushes the hook gates, and how the bypasses read."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("prepush_gate", ROOT / "scripts" / "ci" / "prepush_gate.py")
assert SPEC and SPEC.loader
pg = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pg
SPEC.loader.exec_module(pg)

A, B = "a" * 40, "b" * 40


def run_hook(stdin: str, env: dict[str, str] | None = None, result: int = 0) -> tuple[int, list[tuple]]:
    calls: list[tuple] = []
    pg.gate = lambda base, head, **kw: calls.append((base, head)) or result
    pg.separation_ok = lambda base, head: not (env or {}).get("SEPARATION_FAIL")
    old_stdin, old_env = sys.stdin, dict(os.environ)
    os.environ.pop("LONGHOUSE_PREPUSH_GATE", None)
    os.environ.update({k: v for k, v in (env or {}).items() if k != "SEPARATION_FAIL"})
    sys.stdin = io.StringIO(stdin)
    try:
        return pg.hook([]), calls
    finally:
        sys.stdin = old_stdin
        os.environ.clear()
        os.environ.update(old_env)


def test_only_pushes_to_main_are_gated() -> None:
    status, calls = run_hook(f"refs/heads/topic {A} refs/heads/topic {pg.ZERO}\nrefs/ship/x {A} refs/ship/x {pg.ZERO}\n")
    assert status == 0 and calls == []


def test_a_push_to_main_is_gated_and_its_failure_blocks() -> None:
    status, calls = run_hook(f"refs/heads/x {A} refs/heads/main {pg.ZERO}\n", result=1)
    assert status == 1 and calls == [("origin/main", A)]


def test_deleting_main_is_not_gated() -> None:
    assert run_hook(f"(delete) {pg.ZERO} refs/heads/main {B}\n") == (0, [])


def test_skip_and_done_bypass_but_only_for_the_named_commit() -> None:
    line = f"refs/heads/x {A} refs/heads/main {pg.ZERO}\n"
    assert run_hook(line, {"LONGHOUSE_PREPUSH_GATE": "skip"}, result=1) == (0, [])
    assert run_hook(line, {"LONGHOUSE_PREPUSH_GATE": f"done:{A}"}, result=1) == (0, [])
    status, calls = run_hook(line, {"LONGHOUSE_PREPUSH_GATE": f"done:{B}"}, result=1)
    assert status == 1 and len(calls) == 1


def test_a_separation_violation_blocks_even_with_skip() -> None:
    line = f"refs/heads/x {A} refs/heads/main {pg.ZERO}\n"
    assert run_hook(line, {"SEPARATION_FAIL": "1"}) == (1, [])
    assert run_hook(line, {"SEPARATION_FAIL": "1", "LONGHOUSE_PREPUSH_GATE": "skip"}) == (1, [])
    assert run_hook(line, {"SEPARATION_FAIL": "1", "LONGHOUSE_PREPUSH_GATE": f"done:{A}"}) == (1, [])


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"prepush-gate tests passed ({len(tests)} scenarios)")
