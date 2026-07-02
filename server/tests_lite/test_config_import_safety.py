"""Tests for the import-time config-validation split (Phase 2 root fix).

zerg.database used to call get_settings() at import time, which runs
_validate_required() and raises RuntimeError when DATABASE_URL is unset. That
crashed every remote-only CLI surface that transitively imported the ingest
models (cursor Helm launcher, --version, etc.). The fix: zerg.database uses
the validation-free get_settings_unchecked() at import; the server still
validates via zerg.main's get_settings() call at import. These tests lock in
the routing hermetically (in-process, dotenv-independent) plus one end-to-end
subprocess check.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import zerg.config as config_mod


def _run_without_runtime_config(script: str) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "FERNET_SECRET"}}
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo_root / "server"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_get_settings_unchecked_does_not_call_validate(monkeypatch):
    """get_settings_unchecked() must NOT call _validate_required — it is the
    import-time-safe raw accessor."""
    def _boom(_settings):
        raise AssertionError("_validate_required was called by get_settings_unchecked")

    monkeypatch.setattr(config_mod, "_validate_required", _boom)
    monkeypatch.setattr(config_mod, "_load_settings", lambda: MagicMock(database_url=""))
    # Must not raise (i.e. _boom must not fire).
    s = config_mod.get_settings_unchecked()
    assert s is not None


def test_get_settings_calls_validate(monkeypatch):
    """get_settings() (server accessor) must call _validate_required — the
    fail-fast contract for server-side callers is unchanged."""
    called = {"n": 0}

    def _track(_settings):
        called["n"] += 1

    monkeypatch.setattr(config_mod, "_validate_required", _track)
    monkeypatch.setattr(config_mod, "_load_settings", lambda: MagicMock(database_url="sqlite:///./x.db"))
    config_mod.get_settings()
    assert called["n"] == 1, "get_settings() did not call _validate_required"


def test_validate_required_settings_calls_validate(monkeypatch):
    """The explicit startup preflight must call _validate_required."""
    called = {"n": 0}

    def _track(_settings):
        called["n"] += 1

    monkeypatch.setattr(config_mod, "_validate_required", _track)
    monkeypatch.setattr(config_mod, "_load_settings", lambda: MagicMock(database_url="sqlite:///./x.db"))
    config_mod.validate_required_settings()
    assert called["n"] == 1


def test_zerg_database_uses_unchecked_accessor():
    """zerg.database must use get_settings_unchecked (not get_settings) at
    import — the structural fix that makes it import-safe without DATABASE_URL.
    Source-level guard so a future edit can't silently revert it."""
    repo_root = Path(__file__).resolve().parents[2]
    src = (repo_root / "server" / "zerg" / "database.py").read_text(encoding="utf-8")
    assert "get_settings_unchecked" in src, "zerg.database must import get_settings_unchecked"
    # The validating get_settings() must NOT be imported by zerg.database.
    assert "from zerg.config import get_settings\n" not in src, (
        "zerg.database must not import the validating get_settings() — it would "
        "crash remote-only CLI surfaces at import time again."
    )
    assert "_settings = get_settings_unchecked()" in src
    assert "dotenv.load_dotenv" not in src, "zerg.database must not load dotenv at import time"
    assert "default_engine = make_engine(_settings.database_url)" not in src, (
        "zerg.database must not create the default engine at import time"
    )


def test_zerg_main_still_validates_at_import():
    """zerg.main is the server app module; it must STILL use the validating
    get_settings() at import so server boot fails fast on missing config.
    Source-level guard that the validation was relocated to the server-only
    path, not silently dropped."""
    repo_root = Path(__file__).resolve().parents[2]
    src = (repo_root / "server" / "zerg" / "main.py").read_text(encoding="utf-8")
    assert "_settings = get_settings()" in src, (
        "zerg.main must keep `_settings = get_settings()` at import — that is the "
        "server's fail-fast validation point. Moving it to get_settings_unchecked "
        "would let the server boot with missing DATABASE_URL/FERNET_SECRET."
    )


def test_zerg_main_fails_fast_without_database_url():
    repo_root = Path(__file__).resolve().parents[2]
    stripped = {
        "APP_MODE",
        "AUTH_DISABLED",
        "DATABASE_URL",
        "DEMO_MODE",
        "ENVIRONMENT",
        "FERNET_SECRET",
        "TESTING",
    }
    env = {k: v for k, v in os.environ.items() if k not in stripped}
    result = subprocess.run(
        [sys.executable, "-c", "import zerg.main"],
        cwd=str(repo_root / "server"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr


def test_cli_common_does_not_import_local_health_at_module_load():
    repo_root = Path(__file__).resolve().parents[2]
    src = (repo_root / "server" / "zerg" / "cli" / "_common.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_level_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "zerg.services.local_health"
    ]
    assert not top_level_imports, "managed launcher imports must not load broad local-health diagnostics"


def test_zerg_database_imports_without_database_url():
    """End-to-end: importing zerg.database must not raise when DATABASE_URL is
    absent from the process environment. default_engine is None when no
    DATABASE_URL is configured (the graceful placeholder tests rely on).

    NOTE: dotenv may still load a .env file in some environments, so this test
    is a one-directional guarantee (import never raises) rather than a proof
    that default_engine is None in all environments. The routing tests above
    plus the source-level guards are the hermetic proof of the split.
    """
    result = _run_without_runtime_config("import zerg.database; print('DATABASE_IMPORT_OK')")
    assert result.returncode == 0, (
        f"zerg.database import crashed without DATABASE_URL in os.environ — the "
        f"cursor Helm ingest path would be broken again:\n{result.stderr}"
    )
    assert "DATABASE_IMPORT_OK" in result.stdout


def test_managed_launcher_imports_do_not_require_database_url():
    """Managed provider launchers must stay import-safe on remote-only machines."""
    for module_name, marker in (
        ("zerg.cli.codex", "CODEX_IMPORT_OK"),
        ("zerg.cli.claude", "CLAUDE_IMPORT_OK"),
    ):
        result = _run_without_runtime_config(f"import {module_name}; print('{marker}')")
        assert result.returncode == 0, (
            f"{module_name} import failed without DATABASE_URL — managed launches should not need a local DB:\n"
            f"{result.stderr}"
        )
        assert marker in result.stdout
        combined = f"{result.stdout}\n{result.stderr}"
        assert "DATABASE_URL not set" not in combined


def test_top_level_cli_import_does_not_require_database_url():
    result = _run_without_runtime_config("import zerg.cli.main; print('CLI_IMPORT_OK')")
    assert result.returncode == 0, (
        "Top-level CLI import failed without DATABASE_URL; remote-only CLI surfaces should stay lightweight:\n"
        f"{result.stderr}"
    )
    assert "CLI_IMPORT_OK" in result.stdout


def test_longhouse_version_path_does_not_fail_on_missing_database_url():
    result = _run_without_runtime_config(
        "from typer.testing import CliRunner\n"
        "from zerg.cli.main import app\n"
        "result = CliRunner().invoke(app, ['--version'])\n"
        "print('EXIT', result.exit_code)\n"
        "print(result.output)\n"
        "raise SystemExit(0 if result.exit_code in {0, 2} else result.exit_code)\n"
    )
    assert result.returncode == 0, result.stderr
    combined = f"{result.stdout}\n{result.stderr}"
    assert "DATABASE_URL" not in combined


def test_get_session_factory_without_config_fails_clearly():
    result = _run_without_runtime_config(
        "import zerg.database as db\n"
        "try:\n"
        "    db.get_session_factory()\n"
        "except RuntimeError as exc:\n"
        "    print(str(exc))\n"
        "else:\n"
        "    raise SystemExit('get_session_factory unexpectedly succeeded')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "Database is not configured" in result.stdout


def test_database_import_does_not_configure_engine_even_when_env_has_database_url(tmp_path):
    db_path = tmp_path / "import_safe.db"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import zerg.database as db\n"
            "print('DEFAULT_ENGINE', db.default_engine)\n"
            "print('DEFAULT_FACTORY', db.default_session_factory)\n",
        ],
        cwd=str(Path(__file__).resolve().parents[2] / "server"),
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "FERNET_SECRET": "x" * 44},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "DEFAULT_ENGINE None" in result.stdout
    assert "DEFAULT_FACTORY None" in result.stdout


def test_configure_database_builds_runtime_once(tmp_path):
    db_path = tmp_path / "configured.db"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import zerg.database as db\n"
            "first = db.configure_database()\n"
            "second = db.configure_database()\n"
            "print('SAME_RUNTIME', first is second)\n"
            "print('HAS_ENGINE', db.default_engine is not None)\n"
            "print('HAS_FACTORY', db.get_session_factory() is not None)\n",
        ],
        cwd=str(Path(__file__).resolve().parents[2] / "server"),
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "FERNET_SECRET": "x" * 44},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "SAME_RUNTIME True" in result.stdout
    assert "HAS_ENGINE True" in result.stdout
    assert "HAS_FACTORY True" in result.stdout
