#!/usr/bin/env python3
"""Resolve the catalog schema contract from one exact runtime candidate.

The catalog schema is a runtime artifact contract, not the control-plane
protocol schema.  This helper deliberately reads the candidate's own source
(or a requested git revision) and fails closed when the source does not prove
both reader bounds.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path
from typing import Any


SCHEMA_PATH = Path("server/zerg/catalogd/schema.py")
READER_PATH = Path("server/zerg/catalogd/server.py")


class SchemaResolutionError(RuntimeError):
    """The candidate did not expose an unambiguous catalog schema contract."""


def _source_text(root: Path, path: Path, git_ref: str | None) -> str:
    if git_ref:
        try:
            return subprocess.check_output(
                ["git", "-C", str(root), "show", f"{git_ref}:{path}"],
                text=True,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or "git show failed"
            raise SchemaResolutionError(f"unable to read candidate {path}: {detail}") from exc
    try:
        return (root / path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaResolutionError(f"unable to read candidate {path}") from exc


def _catalog_schema_version(source: str) -> int:
    try:
        tree = ast.parse(source, filename=str(SCHEMA_PATH))
    except SyntaxError as exc:
        raise SchemaResolutionError(f"candidate schema source is not valid Python: {exc}") from exc

    values: list[int] = []
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1:
                target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not isinstance(target, ast.Name) or target.id != "CATALOG_SCHEMA_VERSION":
            continue
        if not isinstance(value, ast.Constant) or type(value.value) is not int or value.value < 0:
            raise SchemaResolutionError("CATALOG_SCHEMA_VERSION must be a non-negative integer literal")
        values.append(value.value)

    if len(values) != 1:
        raise SchemaResolutionError(
            f"candidate must define exactly one CATALOG_SCHEMA_VERSION literal (found {len(values)})"
        )
    return values[0]


def _reader_bounds(source: str) -> None:
    try:
        tree = ast.parse(source, filename=str(READER_PATH))
    except SyntaxError as exc:
        raise SchemaResolutionError(f"candidate reader source is not valid Python: {exc}") from exc

    bounds: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys: dict[str, ast.expr] = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and value is not None:
                keys[key.value] = value
        for name in ("minimum_reader_schema_version", "maximum_reader_schema_version"):
            value = keys.get(name)
            if isinstance(value, ast.Name) and value.id == "CATALOG_SCHEMA_VERSION":
                bounds[name] = value.id

    if set(bounds) != {"minimum_reader_schema_version", "maximum_reader_schema_version"}:
        raise SchemaResolutionError(
            "candidate reader source does not prove exact minimum/maximum catalog reader bounds"
        )
    # The source explicitly uses CATALOG_SCHEMA_VERSION for both bounds.
    return None


def resolve_schema(root: Path, git_ref: str | None = None) -> dict[str, Any]:
    version = _catalog_schema_version(_source_text(root, SCHEMA_PATH, git_ref))
    _reader_bounds(_source_text(root, READER_PATH, git_ref))
    return {
        "schema_version": version,
        "schema_min_reader": version,
        "schema_max_reader": version,
        "schema_authority": str(SCHEMA_PATH),
        "reader_authority": str(READER_PATH),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--git-ref", help="resolve both authority files from this exact git revision")
    parser.add_argument("--github-output", type=Path, help="write numeric fields as GitHub Actions outputs")
    args = parser.parse_args(argv)

    try:
        result = resolve_schema(args.source_root.resolve(), args.git_ref)
    except SchemaResolutionError as exc:
        parser.error(str(exc))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for name in ("schema_version", "schema_min_reader", "schema_max_reader"):
                handle.write(f"{name}={result[name]}\n")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
