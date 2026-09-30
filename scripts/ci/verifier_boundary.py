#!/usr/bin/env python3
"""The verifier/subject import boundary and the verifier digest.

The provider factory's verifier (the oracles and producers that judge a run) must
not depend on the code it judges, and the subject must not depend on the verifier.
This is step 3.3a of `control-plane/docs/specs/provider-factory-findings-loop.md`.

Sides, by path:
  verifier   server/zerg/qa/**  and  scripts/qa/provider-control-e2e-canary.py
  neutral    server/zerg/provider_contract/**  (shared proof vocabulary; none yet)
  subject    every other module under server/zerg/

Any import edge from verifier to subject, subject to verifier, or neutral to either,
(real imports, literal import_module calls, and `from zerg... import` lines inside code
strings the verifier hands to a subprocess) must be listed in scripts/ci/verifier-boundary.allow with a reason. The list is a
ratchet: a new edge fails, a listed edge that no longer exists fails (delete the
line), and with --base REV an entry the base did not already carry fails.

`verifier_digest` is a sha256 over {path: sha256(file bytes)} of the verifier files.
The provider factory computes the same digest over the files it pins
(control-plane provider_factory/verifier.py); both sides carry the same golden vector.

  verifier_boundary.py [check] [--root DIR] [--rev REV] [--base REV]
  verifier_boundary.py digest [--root DIR] [--paths-file FILE]
  verifier_boundary.py replay --since WHEN [--rev REV] [--pinned-file F] [--protected-file F]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, Iterator, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = "scripts/ci/verifier-boundary.allow"

SOURCE_PREFIX = "server/zerg/"
VERIFIER_PREFIX = "server/zerg/qa/"
VERIFIER_FILES = ("scripts/qa/provider-control-e2e-canary.py",)
NEUTRAL_PREFIX = "server/zerg/provider_contract/"

DIGEST_KIND = "provider_factory_verifier_set"

VERIFIER, NEUTRAL, SUBJECT = "verifier", "neutral", "subject"

Edge = tuple[str, str]


# --- sides -----------------------------------------------------------------


def is_verifier_path(path: str) -> bool:
    return path.startswith(VERIFIER_PREFIX) or path in VERIFIER_FILES


def side(path: str) -> str:
    if is_verifier_path(path):
        return VERIFIER
    if path.startswith(NEUTRAL_PREFIX):
        return NEUTRAL
    return SUBJECT


def _ignorable(path: str) -> bool:
    parts = path.split("/")
    return path.endswith(".pyc") or any(part == "__pycache__" or part.startswith(".") for part in parts)


# --- import graph ----------------------------------------------------------


def _module_of(path: str) -> str | None:
    if not (path.startswith(SOURCE_PREFIX) and path.endswith(".py")):
        return None
    module = path[len("server/") : -len(".py")].replace("/", ".")
    return module.removesuffix(".__init__")


def read_sources(root: Path) -> dict[str, str]:
    """Every Python file the boundary covers: server/zerg/**.py and the canary script."""
    paths = [
        path.relative_to(root).as_posix()
        for path in (root / "server" / "zerg").rglob("*.py")
        if not _ignorable(path.relative_to(root).as_posix())
    ]
    paths += [name for name in VERIFIER_FILES if (root / name).is_file()]
    return {path: (root / path).read_text(encoding="utf-8") for path in sorted(paths)}


# An import statement of zerg code that starts a line or follows a `;` (`python -c "import os; from zerg.x import y"`).
_EMBEDDED_IMPORT = re.compile(
    r"(?:^|;)[ \t]*((?:from[ \t]+zerg[\w.]*[ \t]+import\b|import[ \t]+zerg[\w.]*)[^;\n]*)", re.MULTILINE
)


def _import_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Every node of a module, plus the import statements inside embedded Python: a string (or f-string
    chunk) with a statement `from zerg... import ...` or `import zerg...` is code the verifier hands to a
    subprocess, and it depends on the subject as much as a real import does. Only statements that start a
    line or follow a `;` are seen. `from zerg.x import {name}` counts as an import of zerg.x; an interpolated module name
    (`import zerg.{mod}`) or a script assembled from pieces is not seen."""
    for node in ast.walk(tree):
        yield node
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for line in (match.strip() for match in _EMBEDDED_IMPORT.findall(node.value)):
                try:
                    yield from ast.walk(ast.parse(line))
                except SyntaxError:
                    # A line that does not parse alone (`from zerg.x import ` + {names}, or the first line of a
                    # parenthesized list) still names its module.
                    partial = re.match(r"from[ \t]+(zerg[\w.]*)[ \t]+import\b", line)
                    if partial:
                        yield ast.ImportFrom(module=partial.group(1), names=[ast.alias(name="*")], level=0)


def import_edges(sources: Mapping[str, str]) -> set[Edge]:
    """(importer path, imported path) for every import of a zerg module, at any nesting depth, in code
    strings too, and through literal `import_module("zerg...")` calls."""
    modules = {module: path for path in sources if (module := _module_of(path))}

    def target(dotted: str) -> str | None:
        parts = dotted.split(".")
        while parts:
            if (path := modules.get(".".join(parts))) is not None:
                return path
            parts.pop()
        return None

    edges: set[Edge] = set()
    for importer, source in sources.items():
        own = _module_of(importer)
        package = own.split(".") if own and importer.endswith("/__init__.py") else (own or "").split(".")[:-1]
        found: set[str] = set()
        tree = ast.parse(source, filename=importer)
        # `from importlib import import_module as load` still loads modules by name.
        loaders = {"import_module", "__import__"} | {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "importlib"
            for alias in node.names
            if alias.name == "import_module" and alias.asname
        }

        def add(dotted: str) -> None:
            if (dotted == "zerg" or dotted.startswith("zerg.")) and (path := target(dotted)):
                found.add(path)

        for node in _import_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = len(package) - (node.level - 1)
                    if keep <= 0:
                        continue
                    base = ".".join([*package[:keep], *(node.module.split(".") if node.module else [])])
                else:
                    base = node.module or ""
                # `from pkg import name` depends on pkg.name when that is a module, else on pkg itself.
                submodules = [a.name for a in node.names if a.name != "*" and f"{base}.{a.name}" in modules]
                if len(submodules) < len(node.names):
                    add(base)
                for name in submodules:
                    add(f"{base}.{name}")
            elif isinstance(node, ast.Call) and _is_dynamic_import(node.func, loaders):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    add(node.args[0].value)
        found.discard(importer)
        edges.update((importer, path) for path in found)
    return edges


def _is_dynamic_import(func: ast.expr, loaders: set[str | None]) -> bool:
    if isinstance(func, ast.Name):
        return func.id in loaders
    return isinstance(func, ast.Attribute) and func.attr in ("import_module", "__import__")


def crossing_edges(edges: Iterable[Edge]) -> set[Edge]:
    """Edges the boundary forbids: verifier or neutral importers reaching outside their own side,
    subject importers reaching the verifier. Anyone may import the neutral package."""
    crossing = set()
    for importer, imported in edges:
        a, b = side(importer), side(imported)
        if a != b and b != NEUTRAL:
            crossing.add((importer, imported))
    return crossing


def direction(edge: Edge) -> str:
    return f"{side(edge[0])}->{side(edge[1])}"


# --- allowlist -------------------------------------------------------------


def parse_allowlist(text: str) -> tuple[dict[Edge, str], list[str]]:
    entries: dict[Edge, str] = {}
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, sep, reason = line.partition("#")
        importer, arrow, imported = (part.strip() for part in body.partition("->"))
        if not (arrow and importer and imported and not ("->" in imported)):
            problems.append(f"{ALLOWLIST}:{number}: expected `importer -> imported  # reason`, got: {line}")
        elif not (sep and reason.strip()):
            problems.append(f"{ALLOWLIST}:{number}: {importer} -> {imported} has no reason")
        elif (importer, imported) in entries:
            problems.append(f"{ALLOWLIST}:{number}: duplicate entry {importer} -> {imported}")
        else:
            entries[(importer, imported)] = reason.strip()
    return entries, problems


def check_edges(
    edges: Iterable[Edge], allowed: Mapping[Edge, str], base_allowed: Mapping[Edge, str] | None = None
) -> list[str]:
    current = crossing_edges(edges)
    problems = []
    for edge in sorted(current - set(allowed)):
        problems.append(
            f"new {direction(edge)} import: {edge[0]} -> {edge[1]}\n"
            "    The verifier and the subject do not import each other: drive the subject through its CLI or API "
            "(proof vocabulary moves to zerg/provider_contract in 3.3b). The allowlist only shrinks; do not add to it."
        )
    for edge in sorted(set(allowed) - current):
        problems.append(f"stale allowlist entry, the import is gone: delete `{edge[0]} -> {edge[1]}` from {ALLOWLIST}")
    if base_allowed is not None:
        for edge in sorted(set(allowed) - set(base_allowed)):
            problems.append(
                f"{ALLOWLIST} grew: `{edge[0]} -> {edge[1]}` is not in the base; the allowlist only shrinks "
                "(if you did not add it, your branch is behind the base: rebase first)"
            )
    return problems


# --- verifier digest -------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def digest_files(files: Mapping[str, str]) -> str:
    """The verifier digest of {repo-relative path: "sha256:<hex of file bytes>"}."""
    manifest = {
        "schema_version": 1,
        "artifact_kind": DIGEST_KIND,
        "files": {path: files[path] for path in sorted(files)},
    }
    canonical = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def verifier_rule_paths(root: Path) -> list[str]:
    """Every file under the verifier path rule in a working tree."""
    paths = [
        path.relative_to(root).as_posix()
        for path in (root / VERIFIER_PREFIX).rglob("*")
        if path.is_file() and not _ignorable(path.relative_to(root).as_posix())
    ]
    paths += [name for name in VERIFIER_FILES if (root / name).is_file()]
    return sorted(paths)


def verifier_digest(root: Path, paths: Iterable[str] | None = None) -> str:
    """Digest of `paths` (default: the whole verifier path rule) as they are on disk under `root`."""
    chosen = verifier_rule_paths(root) if paths is None else sorted(set(paths))
    return digest_files({path: sha256_file(root / path) for path in chosen})


# --- replay: how often does a pin change across history --------------------

PIN_LABELS = {
    "rule": "verifier path rule (qa/** + canary)",
    "pinned": "pinned verifier set",
    "protected": "protected inputs",
}


def _git(root: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if done.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {done.stderr.decode(errors='replace').strip()[:500]}")
    return done.stdout


def replay(root: Path, rev: str, since: str, sets: Mapping[str, list[str] | None]) -> tuple[int, dict[str, set[str]]]:
    """Non-merge commits reachable from `rev` since `since`, and per named path set the commits whose
    digest differs from their first parent's. A set of None is the verifier path rule."""
    parents: dict[str, str | None] = {}
    for line in _git(root, "rev-list", "--no-merges", "--parents", f"--since={since}", rev).decode().splitlines():
        commit, *rest = line.split()
        parents[commit] = rest[0] if rest else None
    pathspecs = [
        VERIFIER_PREFIX.rstrip("/"),
        *VERIFIER_FILES,
        *sorted({p for chosen in sets.values() for p in chosen or []}),
    ]
    blob_digests: dict[str, str] = {}
    listings: dict[str | None, dict[str, str]] = {None: {}}

    def listing(commit: str | None) -> dict[str, str]:
        if commit not in listings:
            out = _git(root, "ls-tree", "-r", "-z", commit or "", "--", *pathspecs).decode()
            listings[commit] = dict(
                (path, meta.split()[2]) for meta, path in (e.split("\t", 1) for e in out.split("\0") if e)
            )
        return listings[commit]

    def digest(commit: str | None, chosen: list[str] | None) -> str:
        tree = listing(commit)
        wanted = (
            [p for p in tree if is_verifier_path(p) and not _ignorable(p)]
            if chosen is None
            else [p for p in chosen if p in tree]
        )
        for path in wanted:
            if tree[path] not in blob_digests:
                blob_digests[tree[path]] = (
                    "sha256:" + hashlib.sha256(_git(root, "cat-file", "blob", tree[path])).hexdigest()
                )
        return digest_files({path: blob_digests[tree[path]] for path in wanted})

    changed: dict[str, set[str]] = {name: set() for name in sets}
    for commit, parent in parents.items():
        for name, chosen in sets.items():
            if digest(commit, chosen) != digest(parent, chosen):
                changed[name].add(commit)
    return len(parents), changed


# --- cli -------------------------------------------------------------------


def _read_lines(path: str) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _base_allowlist(root: Path, base: str) -> dict[Edge, str] | None:
    done = subprocess.run(["git", "-C", str(root), "show", f"{base}:{ALLOWLIST}"], capture_output=True, text=True)
    if done.returncode:
        if subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"{base}^{{commit}}"], capture_output=True
        ).returncode:
            raise SystemExit(f"--base {base}: not a commit in this checkout")
        print(f"verifier boundary: {base} has no {ALLOWLIST}; skipping the only-shrinks comparison")
        return None
    entries, problems = parse_allowlist(done.stdout)
    if problems:
        raise SystemExit(f"--base {base}: its allowlist does not parse: {problems[0]}")
    return entries


def export_tree(root: Path, rev: str, destination: Path) -> None:
    """Materialize the files the check reads, exactly as commit `rev` has them."""
    wanted = ["server/zerg", *VERIFIER_FILES, ALLOWLIST]
    present = [
        path
        for path in wanted
        if subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{rev}:{path}"], capture_output=True).returncode
        == 0
    ]
    done = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", rev, "--", *present], capture_output=True)
    if done.returncode:
        raise SystemExit(f"--rev {rev}: git archive failed: {done.stderr.decode(errors='replace').strip()[:300]}")
    options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        archive.extractall(destination, **options)


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.root)
    with tempfile.TemporaryDirectory() as scratch:
        tree = root
        if args.rev:
            tree = Path(scratch)
            export_tree(root, args.rev, tree)
        allowlist = tree / ALLOWLIST
        allowed, problems = parse_allowlist(allowlist.read_text(encoding="utf-8") if allowlist.is_file() else "")
        base_allowed = _base_allowlist(root, args.base) if args.base else None
        edges = import_edges(read_sources(tree))
        problems += check_edges(edges, allowed, base_allowed)
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print(f"verifier boundary: {len(problems)} problem(s)", file=sys.stderr)
            return 1
        kinds: dict[str, int] = {}
        for edge in crossing_edges(edges):
            kinds[direction(edge)] = kinds.get(direction(edge), 0) + 1
        summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())) or "none"
        print(f"verifier boundary: crossing edges {summary}, all allowlisted; verifier_digest {verifier_digest(tree)}")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    root = Path(args.root)
    paths = _read_lines(args.paths_file) if args.paths_file else verifier_rule_paths(root)
    print(f"{verifier_digest(root, paths)}  {len(paths)} files")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    sets: dict[str, list[str] | None] = {"rule": None}
    if args.pinned_file:
        sets["pinned"] = _read_lines(args.pinned_file)
    if args.protected_file:
        sets["protected"] = _read_lines(args.protected_file)
    total, changed = replay(Path(args.root), args.rev, args.since, sets)
    print(f"non-merge commits since {args.since} up to {args.rev}: {total}")
    for name, commits in changed.items():
        print(f"  digest changes {len(commits):4d}  {PIN_LABELS[name]}")
    if "pinned" in changed and "protected" in changed:
        print(f"  pin changed, pinned verifier did not: {len(changed['protected'] - changed['pinned'])}")
        print(f"  pinned verifier changed, pin did not: {len(changed['pinned'] - changed['protected'])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "check")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=str(REPO_ROOT), help="checkout to read (default: this one)")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", parents=[common], help="fail on a boundary edge the allowlist does not carry")
    check.add_argument("--rev", help="check the tree of this commit instead of the working tree")
    check.add_argument("--base", help="git revision whose allowlist this one must not exceed")
    digest = sub.add_parser("digest", parents=[common], help="print the verifier digest")
    digest.add_argument("--paths-file", help="digest exactly these repo-relative paths (default: the path rule)")
    rep = sub.add_parser("replay", parents=[common], help="count digest changes across history")
    rep.add_argument("--since", required=True)
    rep.add_argument("--rev", default="HEAD")
    rep.add_argument("--pinned-file", help="one repo-relative path per line: the pinned verifier set")
    rep.add_argument("--protected-file", help="one repo-relative path per line: the protected-input pin")
    args = parser.parse_args(argv)
    return {"check": cmd_check, "digest": cmd_digest, "replay": cmd_replay}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
