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

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import zerg.config as config_mod


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


def test_zerg_database_imports_without_database_url():
    """End-to-end: importing zerg.database must not raise when DATABASE_URL is
    absent from the process environment. default_engine is None when no
    DATABASE_URL is configured (the graceful placeholder tests rely on).

    NOTE: dotenv may still load a .env file in some environments, so this test
    is a one-directional guarantee (import never raises) rather than a proof
    that default_engine is None in all environments. The routing tests above
    plus the source-level guards are the hermetic proof of the split.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "FERNET_SECRET"}}
    result = subprocess.run(
        [sys.executable, "-c", "import zerg.database; print('DATABASE_IMPORT_OK')"],
        cwd=str(repo_root / "server"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"zerg.database import crashed without DATABASE_URL in os.environ — the "
        f"cursor Helm ingest path would be broken again:\n{result.stderr}"
    )
    assert "DATABASE_IMPORT_OK" in result.stdout
