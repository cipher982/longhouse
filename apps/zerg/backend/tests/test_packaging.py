"""Packaging smoke tests — verify the install.sh / pip install path works.

Validates that pyproject.toml metadata, entry points, and key module imports
are well-formed so `uv tool install .` (or `pip install .`) won't fail at
install time.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Locate the backend root (where pyproject.toml lives)
BACKEND_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = BACKEND_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    """Parse pyproject.toml into a dict."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

    return tomllib.loads(PYPROJECT_PATH.read_text())


class TestPyprojectMetadata:
    """Verify pyproject.toml has valid, complete metadata."""

    def test_required_fields_present(self, pyproject: dict) -> None:
        """Package must declare name, version, description, and dependencies."""
        project = pyproject["project"]
        assert project["name"] == "longhouse"
        assert project["version"], "version must be non-empty"
        assert project["description"], "description must be non-empty"
        assert len(project["dependencies"]) > 0, "dependencies must not be empty"

    def test_python_version_constraint(self, pyproject: dict) -> None:
        """Package must require Python 3.12+."""
        requires = pyproject["project"]["requires-python"]
        assert "3.12" in requires

    def test_build_system_defined(self, pyproject: dict) -> None:
        """A PEP 517 build backend must be configured."""
        build = pyproject["build-system"]
        assert "hatchling" in build["requires"]
        assert build["build-backend"] == "hatchling.build"


class TestEntryPoints:
    """Verify CLI entry points are importable."""

    def test_longhouse_script_defined(self, pyproject: dict) -> None:
        """pyproject.toml must declare a 'longhouse' console script."""
        scripts = pyproject["project"]["scripts"]
        assert "longhouse" in scripts
        assert scripts["longhouse"] == "zerg.cli.main:main"

    def test_cli_entry_point_importable(self) -> None:
        """The declared entry point module must be importable."""
        mod = importlib.import_module("zerg.cli.main")
        assert hasattr(mod, "main"), "zerg.cli.main must expose a main() callable"
        assert callable(mod.main)

    def test_typer_app_exists(self) -> None:
        """The CLI app object must exist for typer to invoke."""
        from zerg.cli.main import app

        assert app is not None
        assert app.info.name == "longhouse"


class TestKeyImports:
    """Verify that the core package structure is importable."""

    @pytest.mark.parametrize(
        "module_path",
        [
            "zerg",
            "zerg.cli",
            "zerg.cli.main",
            "zerg.cli.serve",
            "zerg.cli.connect",
        ],
    )
    def test_core_modules_importable(self, module_path: str) -> None:
        """Key modules in the package tree must be importable."""
        mod = importlib.import_module(module_path)
        assert mod is not None
