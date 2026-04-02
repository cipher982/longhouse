"""Tests for CLI wrapper shim install / uninstall / status."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from zerg.services.shipper.wrappers import (
    SUPPORTED_PROVIDERS,
    _MARKER_BEGIN,
    _MARKER_END,
    _build_shim_script,
    _find_real_binary,
    _inject_profile_block,
    _profile_has_block,
    _remove_profile_block,
    _shims_dir,
    get_wrapper_status,
    install_wrappers,
    uninstall_wrappers,
)


# ------------------------------------------------------------------
# Shim script generation
# ------------------------------------------------------------------


class TestBuildShimScript:
    def test_contains_provider_name(self):
        script = _build_shim_script("claude")
        assert "longhouse claude" in script
        assert "longhouse codex" not in script

    def test_codex_variant(self):
        script = _build_shim_script("codex")
        assert "longhouse codex" in script
        assert "longhouse claude" not in script

    def test_has_bypass_check(self):
        script = _build_shim_script("claude")
        assert "LONGHOUSE_BYPASS" in script

    def test_has_tty_check(self):
        script = _build_shim_script("claude")
        assert "! -t 0" in script

    def test_passthrough_subcommands(self):
        script = _build_shim_script("claude")
        assert "auth" in script
        assert "config" in script
        assert "mcp" in script

    def test_shebang(self):
        script = _build_shim_script("claude")
        assert script.startswith("#!/usr/bin/env bash\n")

    def test_bare_invocation_routes_to_longhouse(self):
        """v1: only bare invocations (zero args) go to longhouse."""
        script = _build_shim_script("claude")
        assert '$# -gt 0' in script

    def test_self_recursion_guard(self):
        """Shim should detect and abort if it resolves back to itself."""
        script = _build_shim_script("claude")
        assert "_THIS_SCRIPT" in script
        assert "_REAL_CANON" in script


# ------------------------------------------------------------------
# Shell profile block management
# ------------------------------------------------------------------


class TestProfileBlock:
    def test_inject_creates_block(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# existing content\n")
        result = _inject_profile_block(profile)
        assert result is True
        text = profile.read_text()
        assert _MARKER_BEGIN in text
        assert _MARKER_END in text
        assert ".longhouse/shims" in text

    def test_inject_idempotent(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# existing\n")
        _inject_profile_block(profile)
        result = _inject_profile_block(profile)
        assert result is False
        assert profile.read_text().count(_MARKER_BEGIN) == 1

    def test_remove_block(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# before\n")
        _inject_profile_block(profile)
        assert _profile_has_block(profile) is True
        result = _remove_profile_block(profile)
        assert result is True
        text = profile.read_text()
        assert _MARKER_BEGIN not in text
        assert "# before" in text

    def test_remove_nonexistent(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# plain\n")
        assert _remove_profile_block(profile) is False

    def test_remove_missing_file(self, tmp_path: Path):
        assert _remove_profile_block(tmp_path / "nope") is False

    def test_has_block_false_on_missing(self, tmp_path: Path):
        assert _profile_has_block(tmp_path / "nope") is False

    def test_posix_block_is_source_safe(self, tmp_path: Path):
        """The injected block should guard against repeated PATH prepend."""
        profile = tmp_path / ".zshrc"
        profile.write_text("# existing\n")
        _inject_profile_block(profile)
        text = profile.read_text()
        # Should use case guard, not raw export
        assert 'case ":$PATH:"' in text


# ------------------------------------------------------------------
# Install / uninstall / status (with monkeypatched HOME)
# ------------------------------------------------------------------


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect HOME and shell config to a temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")

    # Create a fake .zshrc
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("# zshrc\n")

    # Create fake binaries on PATH so _find_real_binary works
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    for name in ("claude", "codex", "longhouse"):
        p = fake_bin / name
        p.write_text("#!/bin/sh\necho fake\n")
        p.chmod(stat.S_IRWXU)
    # Isolate PATH to only fake binaries (avoid leaking real system binaries)
    monkeypatch.setenv("PATH", str(fake_bin))

    # Patch _longhouse_home and _shims_dir to use tmp
    import zerg.services.shipper.wrappers as mod

    monkeypatch.setattr(mod, "_longhouse_home", lambda: tmp_path / ".longhouse")
    monkeypatch.setattr(mod, "_shims_dir", lambda: tmp_path / ".longhouse" / "shims")
    monkeypatch.setattr(mod, "_get_shell_profile_path", lambda: zshrc)

    return tmp_path


class TestInstallWrappers:
    def test_install_creates_shims(self, fake_home: Path):
        results = install_wrappers()
        shims = fake_home / ".longhouse" / "shims"
        assert (shims / "claude").exists()
        assert (shims / "codex").exists()
        # Shims should be executable
        assert os.access(shims / "claude", os.X_OK)
        assert "installed" in results["claude"]

    def test_install_single_provider(self, fake_home: Path):
        results = install_wrappers(providers=["claude"])
        shims = fake_home / ".longhouse" / "shims"
        assert (shims / "claude").exists()
        assert not (shims / "codex").exists()

    def test_install_injects_profile(self, fake_home: Path):
        install_wrappers()
        zshrc = fake_home / ".zshrc"
        assert _MARKER_BEGIN in zshrc.read_text()

    def test_install_skips_missing_binary(self, fake_home: Path, monkeypatch: pytest.MonkeyPatch):
        # Remove codex from fake bin
        (fake_home / "fakebin" / "codex").unlink()
        results = install_wrappers()
        assert "skipped" in results["codex"]

    def test_install_skips_profile_when_no_shims(self, fake_home: Path, monkeypatch: pytest.MonkeyPatch):
        """Profile block should NOT be injected if all providers were skipped."""
        (fake_home / "fakebin" / "claude").unlink()
        (fake_home / "fakebin" / "codex").unlink()
        results = install_wrappers()
        assert "skipped" in results["profile"]
        zshrc = fake_home / ".zshrc"
        assert _MARKER_BEGIN not in zshrc.read_text()

    def test_invalid_provider_raises(self, fake_home: Path):
        with pytest.raises(ValueError, match="Unsupported provider"):
            install_wrappers(providers=["gemini"])


class TestUninstallWrappers:
    def test_uninstall_removes_shims(self, fake_home: Path):
        install_wrappers()
        results = uninstall_wrappers()
        shims = fake_home / ".longhouse" / "shims"
        assert not (shims / "claude").exists()
        assert not (shims / "codex").exists()
        assert "removed" in results["claude"]

    def test_uninstall_removes_profile_block(self, fake_home: Path):
        install_wrappers()
        uninstall_wrappers()
        zshrc = fake_home / ".zshrc"
        assert _MARKER_BEGIN not in zshrc.read_text()

    def test_uninstall_partial_keeps_profile(self, fake_home: Path):
        install_wrappers()
        # Remove only claude
        uninstall_wrappers(providers=["claude"])
        zshrc = fake_home / ".zshrc"
        # Profile block should remain because codex shim still exists
        assert _MARKER_BEGIN in zshrc.read_text()

    def test_uninstall_when_not_installed(self, fake_home: Path):
        results = uninstall_wrappers()
        assert "not installed" in results["claude"]


class TestGetWrapperStatus:
    def test_status_not_installed(self, fake_home: Path):
        status = get_wrapper_status()
        assert status["claude"]["installed"] is False
        assert status["codex"]["installed"] is False
        assert status["profile"]["installed"] is False

    def test_status_after_install(self, fake_home: Path):
        install_wrappers()
        status = get_wrapper_status()
        assert status["claude"]["installed"] is True
        assert status["codex"]["installed"] is True
        assert status["profile"]["installed"] is True

    def test_status_shows_real_binary(self, fake_home: Path):
        status = get_wrapper_status()
        assert "fakebin" in str(status["claude"]["real_binary"])


# ------------------------------------------------------------------
# find_real_binary
# ------------------------------------------------------------------


class TestFindRealBinary:
    def test_finds_binary(self, fake_home: Path):
        path = _find_real_binary("claude")
        assert path is not None
        assert "fakebin" in path

    def test_ignores_shims_dir(self, fake_home: Path):
        # Install wrappers (creates shims)
        install_wrappers()
        # _find_real_binary should still return the fakebin, not the shim
        path = _find_real_binary("claude")
        assert path is not None
        assert ".longhouse/shims" not in path

    def test_returns_none_for_missing(self, fake_home: Path):
        assert _find_real_binary("nonexistent-tool") is None
