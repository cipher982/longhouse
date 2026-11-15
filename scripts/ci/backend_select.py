#!/usr/bin/env python3
"""Pick the backend tests (server/tests_lite) a change can affect, by Python import graph.

Used by scripts/ci/affected.py for the pre-push gate. The goal is a smaller run than the full suite that still holds
every test that could fail, so every doubt resolves to the full suite:

- Edges are static imports (``import x``, ``from x import y``, relative imports) and every string constant that names
  a ``zerg.*`` module (``patch("zerg.routers.x.f")``, ``importlib.import_module``, ``python -m zerg.cli.y``).
- A test is selected when it reaches a changed module through those edges, transitively.
- A changed file that is not a module (data, docs, schemas, engine sources) selects the tests whose source names its
  path or one of its parent directories of two or more components.
- Full suite, with the reason, for: schemas/**, config/**, server/zerg/models/**, database and migration modules,
  server/zerg/generated/**, shared test fixtures (conftest.py, tests_lite/fixtures/**, non-test data files),
  server/pyproject.toml, uv.lock and the runner script, a changed file that does not parse, and any selection that
  holds more than half of the suite (a partial run saves little there and a miss costs a red main).

Usage: backend_select.py [--root R] [--json] PATH...
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

FULL_PATTERNS = (
    (re.compile(r"^schemas/"), "schemas/** feed generated contracts the tests read"),
    (re.compile(r"^config/"), "config/** is read at import time by many modules"),
    (re.compile(r"^server/zerg/models/"), "ORM models are shared by most tests"),
    (re.compile(r"^server/zerg/(database|db_migrations|bootstrap_sqlite)\.py$"), "database and migration modules"),
    (re.compile(r"^server/zerg/generated/"), "generated SDK shared by tests"),
    (re.compile(r"^server/tests_lite/conftest\.py$"), "shared conftest"),
    (re.compile(r"^server/tests_lite/fixtures/"), "shared fixtures"),
    (re.compile(r"^server/(pyproject\.toml|uv\.lock|run_backend_tests_lite\.sh|setup\.cfg|hatch_build\.py)$"), "test runtime"),
    (re.compile(r"^scripts/qa/test[-_]isolation"), "test isolation wrapper"),
)
_ZERG_STRING = re.compile(r"\b(zerg(?:\.[A-Za-z_][A-Za-z0-9_]*)+)")
FULL_FRACTION = 0.5


@dataclass
class Selection:
    full: bool
    reason: str
    tests: list[str] = field(default_factory=list)
    total: int = 0

    def as_dict(self) -> dict:
        return {"full": self.full, "reason": self.reason, "tests": self.tests, "total_tests": self.total}


def _module_name(root: Path, path: Path) -> str | None:
    rel = path.relative_to(root / "server")
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or parts[0] not in ("zerg", "tests_lite"):
        return None
    return ".".join(parts)


def _resolve(name: str, modules: set[str]) -> str | None:
    """The longest known module that is `name` or a dotted prefix of it."""
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in modules:
            return candidate
        parts.pop()
    return None


def _imports(tree: ast.AST, module: str, is_package: bool, modules: set[str]) -> set[str]:
    found: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (hit := _resolve(alias.name, modules)):
                    found.add(hit)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".") if package else []
                if node.level > 1:
                    base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if base and (hit := _resolve(base, modules)):
                found.add(hit)
            for alias in node.names:
                if base and (hit := _resolve(f"{base}.{alias.name}", modules)):
                    found.add(hit)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "zerg" in node.value:
            for match in _ZERG_STRING.finditer(node.value):
                if (hit := _resolve(match.group(1), modules)):
                    found.add(hit)
    return found


def build_graph(root: Path) -> tuple[dict[str, set[str]], dict[str, Path], dict[str, str]]:
    """(reverse edges module -> importers, module -> path, test path -> source text)."""
    server = root / "server"
    paths = sorted(p for base in ("zerg", "tests_lite") for p in (server / base).rglob("*.py"))
    modules: dict[str, Path] = {}
    for path in paths:
        name = _module_name(root, path)
        if name:
            modules[name] = path
    names = set(modules)
    reverse: dict[str, set[str]] = {name: set() for name in names}
    sources: dict[str, str] = {}
    for name, path in modules.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        if name.startswith("tests_lite"):
            sources[name] = text
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue  # a changed file that does not parse is a full run; an unchanged one imports nothing here
        for dep in _imports(tree, name, path.name == "__init__.py", names):
            if dep != name:
                reverse[dep].add(name)
    return reverse, modules, sources


def _is_test_file(name: str) -> bool:
    return name.startswith("tests_lite.") and name.rpartition(".")[2].startswith("test_")


def select(root: Path, changed: list[str]) -> Selection:
    root = root.resolve()
    reverse, modules, sources = build_graph(root)
    all_tests = sorted(n for n in modules if _is_test_file(n))
    total = len(all_tests)
    if not changed:
        return Selection(False, "no changed files", [], total)

    for path in changed:
        for pattern, why in FULL_PATTERNS:
            if pattern.search(path):
                return Selection(True, f"{path}: {why}", [], total)

    selected: set[str] = set()
    seeds: set[str] = set()
    for path in changed:
        full_path = root / path
        if path.endswith(".py") and path.startswith(("server/zerg/", "server/tests_lite/")):
            name = _module_name(root, full_path)
            if full_path.exists():
                try:
                    ast.parse(full_path.read_text(encoding="utf-8"))
                except SyntaxError:
                    return Selection(True, f"{path}: does not parse", [], total)
            if name is None:
                return Selection(True, f"{path}: not a known module", [], total)
            if name not in modules:  # deleted or moved: every importer of the old name may break
                stem = name
                importers = {m for m in modules if m.startswith("tests_lite.") and stem.split(".")[-1] in sources.get(m, "")}
                selected |= {m for m in importers if _is_test_file(m)}
                continue
            seeds.add(name)
            continue
        if path.startswith("server/tests_lite/"):
            return Selection(True, f"{path}: shared test data file", [], total)
        # Any other file (server data, scripts, engine, web, docs): tests that name its path or a parent directory.
        needles = [path]
        parts = path.split("/")[:-1]
        for i in range(len(parts), 1, -1):
            needles.append("/".join(parts[:i]) + "/")
        for test in all_tests:
            text = sources[test]
            if any(n in text for n in needles):
                selected.add(test)

    # Transitive importers of every seed.
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(reverse.get(current, ()))
    selected |= {m for m in seen if _is_test_file(m)}

    tests = sorted(str(modules[m].relative_to(root / "server")) for m in selected if m in modules)
    if total and len(tests) > FULL_FRACTION * total:
        return Selection(True, f"{len(tests)} of {total} tests reach the change", [], total)
    return Selection(False, f"import graph: {len(tests)} of {total} tests reach {len(seeds)} changed module(s)", tests, total)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    result = select(args.root, args.paths)
    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print(("FULL: " if result.full else "") + result.reason)
        for test in result.tests:
            print(test)
    return 0


if __name__ == "__main__":
    sys.exit(main())
