#!/usr/bin/env python3
"""Focused, local-only tests for scripts/ci/affected.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("affected", ROOT / "scripts" / "ci" / "affected.py")
assert SPEC and SPEC.loader
affected = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = affected
SPEC.loader.exec_module(affected)


FILTERS = affected._load_filters(ROOT / ".github" / "path-filters.yml")


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def resolve(*paths: str) -> dict[str, Any]:
    return affected.resolve(FILTERS, {"synthetic": list(paths)})


def test_web_styling_does_not_select_backend_or_engine() -> None:
    result = resolve("web/src/components/Panel.css")
    assert "backend" not in result["categories"]
    assert "engine" not in result["categories"]
    assert "frontend" in result["categories"]
    assert "cube-fast" in result["lanes"]


def test_shared_manifest_selects_every_relevant_filter() -> None:
    result = resolve("server/zerg/config/managed_provider_contracts.json")
    for category in ("backend", "engine", "packaging", "e2e", "runtime_image", "deploy_verify"):
        assert category in result["categories"], (category, result)
    assert "cube-deploy" in result["lanes"]
    assert {row["command"] for row in result["commands"]} >= {
        "make test",
        "make test-engine",
        "make test-wheel-package",
        "make test-e2e",
    }


def test_dirty_and_nonignored_untracked_paths_are_collected() -> None:
    with tempfile.TemporaryDirectory(prefix="affected-git-") as raw:
        repo = Path(raw)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "affected@example.invalid")
        git(repo, "config", "user.name", "affected-test")
        (repo / "web").mkdir()
        (repo / "server").mkdir()
        (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (repo / "web" / "base.css").write_text("base\n", encoding="utf-8")
        (repo / "server" / "base.py").write_text("base\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")
        base = git(repo, "rev-parse", "HEAD")

        (repo / "server" / "staged.py").write_text("staged\n", encoding="utf-8")
        git(repo, "add", "server/staged.py")
        (repo / "web" / "base.css").write_text("unstaged\n", encoding="utf-8")
        (repo / "runner.txt").write_text("untracked\n", encoding="utf-8")
        (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")

        sources = affected.collect_paths(repo, base, "HEAD")
        assert sources["staged"] == ["server/staged.py"]
        assert sources["unstaged"] == ["web/base.css"]
        assert sources["untracked"] == ["runner.txt"]
        assert "ignored.txt" not in sources["untracked"]
        result = affected.resolve(FILTERS, sources)
        assert "backend" in result["categories"]
        assert "frontend" in result["categories"]


def test_unknown_path_gets_conservative_lane() -> None:
    result = resolve("totally-new-surface/file.dat")
    assert result["unknown_paths"] == ["totally-new-surface/file.dat"]
    assert result["lanes"] == ["cube-maint"]
    assert result["commands"] == []


def test_run_refuses_unmatched_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="affected-run-") as raw:
        repo = Path(raw)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "affected@example.invalid")
        git(repo, "config", "user.name", "affected-test")
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")
        base = git(repo, "rev-parse", "HEAD")
        (repo / "README.local").write_text("unmatched\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/ci/affected.py"),
                "--repo",
                str(repo),
                "--filters",
                str(ROOT / ".github/path-filters.yml"),
                "--base",
                base,
                "--head",
                "HEAD",
                "--run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert "unmatched paths" in result.stderr
        assert result.stdout == ""


def test_missing_base_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="affected-empty-") as raw:
        repo = Path(raw)
        git(repo, "init", "-q")
        try:
            affected.collect_paths(repo, "refs/heads/absent", "HEAD")
        except affected.ResolverError as exc:
            assert "BASE" in str(exc)
        else:
            raise AssertionError("missing BASE unexpectedly resolved")


def test_unknown_filter_pattern_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="affected-filters-") as raw:
        path = Path(raw) / "filters.yml"
        path.write_text("frontend:\n  - '[!a].ts'\n", encoding="utf-8")
        try:
            affected._load_filters(path)
        except affected.ResolverError as exc:
            assert "unsupported pattern" in str(exc)
        else:
            raise AssertionError("unsupported filter pattern unexpectedly accepted")


def test_cli_json_is_machine_readable() -> None:
    with tempfile.TemporaryDirectory(prefix="affected-cli-") as raw:
        repo = Path(raw)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "affected@example.invalid")
        git(repo, "config", "user.name", "affected-test")
        (repo / "web").mkdir()
        (repo / "web" / "style.css").write_text("a{}\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")
        (repo / "web" / "new-style.css").write_text("a{}\n", encoding="utf-8")
        base = git(repo, "rev-parse", "HEAD")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/ci/affected.py"),
                "--repo",
                str(repo),
                "--filters",
                str(ROOT / ".github/path-filters.yml"),
                "--base",
                base,
                "--head",
                "HEAD",
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["files"] == ["web/new-style.css"], payload
        assert "frontend" in payload["categories"]
        human = subprocess.run(
            [argument for argument in result.args if argument != "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert human.returncode == 0, human.stderr
        assert "Affected files (1;" in human.stdout
        assert "web/new-style.css" in human.stdout
        assert "CI lanes: " in human.stdout
        assert "cube-fast" in human.stdout


if __name__ == "__main__":
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"affected resolver tests passed ({len(tests)} scenarios)")
