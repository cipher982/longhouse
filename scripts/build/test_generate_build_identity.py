"""Unit tests for generate_build_identity.

Run with: `uv run --no-project pytest scripts/build/test_generate_build_identity.py`
or plain `python -m pytest scripts/build/test_generate_build_identity.py`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Import the module by path so the test works without a package.
import importlib.util

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "generate_build_identity",
    _HERE / "generate_build_identity.py",
)
assert _SPEC is not None and _SPEC.loader is not None
gbi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gbi)


def _init_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo with a single committed file and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("a\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


def _write_pyproject(path: Path, version: str) -> Path:
    pyproject = path / "pyproject.toml"
    pyproject.write_text(f'[project]\nname = "fake"\nversion = "{version}"\n')
    return pyproject


class TestReadVersion:
    def test_reads_version(self, tmp_path: Path) -> None:
        pyproject = _write_pyproject(tmp_path, "1.2.3")
        assert gbi.read_version(pyproject) == "1.2.3"

    def test_missing_version_raises(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = \"fake\"\n")
        with pytest.raises(RuntimeError, match="no version line"):
            gbi.read_version(pyproject)


class TestResolveCommit:
    def test_prefers_github_sha(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        sha = "a" * 40
        assert gbi.resolve_commit(repo, {"GITHUB_SHA": sha}) == sha

    def test_falls_back_to_git_rev_parse(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert gbi.resolve_commit(repo, {}) == head

    def test_ignores_empty_github_sha(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert gbi.resolve_commit(repo, {"GITHUB_SHA": ""}) == head


class TestResolveDirty:
    def test_clean_tree(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        assert gbi.resolve_dirty(repo, {}) is False

    def test_tracked_edit_is_dirty(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "tracked.txt").write_text("b\n")
        assert gbi.resolve_dirty(repo, {}) is True

    def test_untracked_file_is_not_dirty(self, tmp_path: Path) -> None:
        """Shared-worktree reality: other agents' WIP should not pollute provenance."""
        repo = _init_repo(tmp_path)
        (repo / "untracked.txt").write_text("someone else's WIP\n")
        assert gbi.resolve_dirty(repo, {}) is False

    def test_ci_always_clean(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        (repo / "tracked.txt").write_text("edit\n")
        assert gbi.resolve_dirty(repo, {"GITHUB_SHA": "abc"}) is False


class TestResolveChannel:
    @pytest.mark.parametrize(
        "ref,expected",
        [
            ("refs/tags/v0.2.0", "release"),
            ("refs/tags/v1.0.0-rc1", "release"),
            ("refs/heads/main", "dev"),
            ("refs/tags/not-a-version", "dev"),
            ("", "dev"),
        ],
    )
    def test_channel(self, ref: str, expected: str) -> None:
        assert gbi.resolve_channel({"GITHUB_REF": ref}) == expected


class TestBuildIdentity:
    def test_end_to_end_local_dev(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        pyproject = _write_pyproject(repo, "0.2.0")
        fixed_now = datetime(2026, 4, 21, 18, 3, 12, tzinfo=timezone.utc)

        identity = gbi.build_identity(
            repo_root=repo,
            pyproject_path=pyproject,
            env={},
            now=fixed_now,
        )

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert identity == {
            "version": "0.2.0",
            "commit": head,
            "commit_short": head[:8],
            "dirty": False,
            "built_at": "2026-04-21T18:03:12Z",
            "channel": "dev",
        }

    def test_end_to_end_ci_release(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        pyproject = _write_pyproject(repo, "0.2.0")
        fixed_now = datetime(2026, 4, 21, 18, 3, 12, tzinfo=timezone.utc)
        sha = "b672fccae990c020de56139d38dcd9990bae7aa0"

        identity = gbi.build_identity(
            repo_root=repo,
            pyproject_path=pyproject,
            env={"GITHUB_SHA": sha, "GITHUB_REF": "refs/tags/v0.2.0"},
            now=fixed_now,
        )

        assert identity["version"] == "0.2.0"
        assert identity["commit"] == sha
        assert identity["commit_short"] == "b672fcca"
        assert identity["dirty"] is False
        assert identity["built_at"] == "2026-04-21T18:03:12Z"
        assert identity["channel"] == "release"

    def test_dirty_local_build(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        pyproject = _write_pyproject(repo, "0.2.0")
        (repo / "tracked.txt").write_text("edit\n")

        identity = gbi.build_identity(
            repo_root=repo,
            pyproject_path=pyproject,
            env={},
        )
        assert identity["dirty"] is True
        assert identity["channel"] == "dev"


class TestWriteIdentity:
    def test_writes_json_and_creates_parent(self, tmp_path: Path) -> None:
        identity = {
            "version": "0.2.0",
            "commit": "a" * 40,
            "commit_short": "aaaaaaaa",
            "dirty": False,
            "built_at": "2026-04-21T18:03:12Z",
            "channel": "dev",
        }
        out = tmp_path / "nested" / "dir" / "build-identity.json"
        gbi.write_identity(identity, out)

        assert out.exists()
        assert json.loads(out.read_text()) == identity
        # trailing newline for unix hygiene
        assert out.read_text().endswith("\n")


class TestMain:
    def test_main_writes_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _init_repo(tmp_path)
        pyproject = _write_pyproject(repo, "9.9.9")
        # Point the module's REPO_ROOT/PYPROJECT at our fake repo.
        monkeypatch.setattr(gbi, "REPO_ROOT", repo)
        monkeypatch.setattr(gbi, "PYPROJECT_PATH", pyproject)

        out = tmp_path / "out.json"
        rc = gbi.main(["--output", str(out)])

        assert rc == 0
        data = json.loads(out.read_text())
        assert data["version"] == "9.9.9"
        assert data["channel"] == "dev"
        assert data["dirty"] is False
