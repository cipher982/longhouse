"""Every producer failure path settles its verdict through one rule.

A failed result keeps an observation and assertions only when the run reached a
failing verdict (``zerg.qa.failed_results.settle_failed_result``). A failure path
that writes ``observation`` or ``assertions`` itself can synthesize a False the
factory files as a product finding, or an all-true map it refuses as a
contradiction. This scans every ``except`` block of the factory producers under
``server/zerg/qa`` (modules declaring a ``REGISTRATION``) for a failure built that way.

Scope: it reads failure dicts and ``failure[...]``/``result[...]`` assignments in
``except`` blocks. A failure path that writes neither key (a typed harness failure,
or ``partial_observation`` as the OMP background producer does) is already right;
failure helpers outside a handler are covered by their own tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

QA_ROOT = Path(__file__).resolve().parents[1] / "zerg" / "qa"

# Failure paths that still set a verdict themselves, each with the reason it is
# not settled yet. This list only shrinks.
ALLOWED: set[tuple[str, str]] = set()

_VERDICT_KEYS = {"observation", "assertions"}


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is target for child in ast.walk(node)):
                name = node.name
                # Prefer the innermost function.
                inner = _enclosing_function(ast.Module(body=node.body, type_ignores=[]), target)
                return inner or name
    return ""


def _verdict_writes(handler: ast.ExceptHandler) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            if "failure_code" in keys and keys & _VERDICT_KEYS:
                lines.append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in _VERDICT_KEYS
                    and isinstance(target.value, ast.Name)
                    and target.value.id in {"failure", "result"}
                ):
                    lines.append(node.lineno)
    return lines


def _offending_failure_paths() -> dict[tuple[str, str], list[int]]:
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(QA_ROOT.glob("*.py")):
        if path.name == "factory_registration.py":
            continue
        source = path.read_text(encoding="utf-8")
        # Factory producers declare a REGISTRATION; other modules (oracle
        # payloads, the universal harness package) do not write factory results.
        if "\nREGISTRATION = " not in source:
            continue
        tree = ast.parse(source)
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            lines = _verdict_writes(handler)
            if lines:
                key = (path.name, _enclosing_function(tree, handler))
                found.setdefault(key, []).extend(lines)
    return found


def test_failure_paths_settle_their_verdict_through_one_rule() -> None:
    offending = {key: lines for key, lines in _offending_failure_paths().items() if key not in ALLOWED}
    assert not offending, (
        "A producer failure path sets observation/assertions itself; finish it with "
        f"factory_registration.settle_failed_result instead: {offending}"
    )


def test_the_allowlist_names_only_paths_that_still_need_it() -> None:
    stale = ALLOWED - set(_offending_failure_paths())
    assert not stale, f"These failure paths now settle through the rule; remove them from ALLOWED: {stale}"


def test_producers_take_the_helper_from_the_leaf_module() -> None:
    """The factory pins every module a producer imports, so the helper comes from failed_results."""

    wrong: list[str] = []
    for path in sorted(QA_ROOT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "settle_failed_result" not in source or path.name == "failed_results.py":
            continue
        tree = ast.parse(source)
        sources = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and any(alias.name == "settle_failed_result" for alias in node.names)
        }
        if sources != {"zerg.qa.failed_results"}:
            wrong.append(f"{path.name}: {sorted(source for source in sources if source)}")
    assert not wrong, f"import settle_failed_result from zerg.qa.failed_results: {wrong}"
