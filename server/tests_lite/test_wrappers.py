"""Tests for CLI wrapper function install / uninstall / status."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from zerg.services.shipper.wrappers import (
    EXIT_SETUP_FAILED,
    SUPPORTED_PROVIDERS,
    _find_real_binary,
    _inject_profile_block,
    _marker_begin,
    _marker_end,
    _profile_has_block,
    _remove_profile_block,
    build_shell_function,
    get_wrapper_status,
    install_wrappers,
    uninstall_wrappers,
)


# ------------------------------------------------------------------
# Shell function generation
# ------------------------------------------------------------------


class TestBuildShellFunction:
    def test_contains_provider_name(self):
        func = build_shell_function("claude")
        assert "longhouse claude" in func
        assert "longhouse codex" not in func

    def test_codex_variant(self):
        func = build_shell_function("codex")
        assert "longhouse codex" in func
        assert "longhouse claude" not in func

    def test_has_bypass_check(self):
        func = build_shell_function("claude")
        assert "LONGHOUSE_BYPASS" in func

    def test_has_tty_check(self):
        func = build_shell_function("claude")
        assert "! -t 0" in func

    def test_passthrough_subcommands(self):
        func = build_shell_function("claude")
        assert "auth" in func
        assert "config" in func
        assert "mcp" in func

    def test_uses_command_builtin(self):
        """Shell functions should use 'command' to bypass themselves."""
        func = build_shell_function("claude")
        assert "command claude" in func

    def test_bare_invocation_routes_to_longhouse(self):
        """v1: only bare invocations (zero args) go to longhouse."""
        func = build_shell_function("claude")
        assert "$# -gt 0" in func

    def test_fallback_on_setup_failure(self):
        """Should fall back to native CLI when longhouse exits with EXIT_SETUP_FAILED."""
        func = build_shell_function("claude")
        assert str(EXIT_SETUP_FAILED) in func
        assert "managed launch unavailable" in func

    def test_defines_function(self):
        func = build_shell_function("claude")
        assert "claude()" in func

    def test_has_markers(self):
        func = build_shell_function("claude")
        assert _marker_begin("claude") in func
        assert _marker_end("claude") in func

    def test_fish_variant(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        func = build_shell_function("claude")
        assert "function claude" in func
        assert "command claude" in func
        assert "isatty" in func


# ------------------------------------------------------------------
# Profile block management (per-provider)
# ------------------------------------------------------------------


class TestProfileBlock:
    def test_inject_creates_block(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# existing content\n")
        result = _inject_profile_block(profile, "claude")
        assert result is True
        text = profile.read_text()
        assert _marker_begin("claude") in text
        assert _marker_end("claude") in text
        assert "command claude" in text

    def test_inject_idempotent(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# existing\n")
        _inject_profile_block(profile, "claude")
        result = _inject_profile_block(profile, "claude")
        assert result is False
        assert profile.read_text().count(_marker_begin("claude")) == 1

    def test_inject_multiple_providers(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# existing\n")
        _inject_profile_block(profile, "claude")
        _inject_profile_block(profile, "codex")
        text = profile.read_text()
        assert _marker_begin("claude") in text
        assert _marker_begin("codex") in text

    def test_remove_single_provider(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# before\n")
        _inject_profile_block(profile, "claude")
        _inject_profile_block(profile, "codex")
        # Remove only claude
        result = _remove_profile_block(profile, "claude")
        assert result is True
        text = profile.read_text()
        assert _marker_begin("claude") not in text
        # Codex should remain
        assert _marker_begin("codex") in text

    def test_remove_nonexistent(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# plain\n")
        assert _remove_profile_block(profile, "claude") is False

    def test_remove_missing_file(self, tmp_path: Path):
        assert _remove_profile_block(tmp_path / "nope", "claude") is False

    def test_has_block_false_on_missing(self, tmp_path: Path):
        assert _profile_has_block(tmp_path / "nope", "claude") is False

    def test_has_block_provider_specific(self, tmp_path: Path):
        profile = tmp_path / ".zshrc"
        profile.write_text("# existing\n")
        _inject_profile_block(profile, "claude")
        assert _profile_has_block(profile, "claude") is True
        assert _profile_has_block(profile, "codex") is False


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

    import zerg.services.shipper.wrappers as mod

    monkeypatch.setattr(mod, "_get_shell_profile_path", lambda: zshrc)

    return tmp_path


class TestInstallWrappers:
    def test_install_injects_functions(self, fake_home: Path):
        results = install_wrappers()
        zshrc = fake_home / ".zshrc"
        text = zshrc.read_text()
        assert _marker_begin("claude") in text
        assert _marker_begin("codex") in text
        assert "installed" in results["claude"]
        assert "installed" in results["codex"]

    def test_install_single_provider(self, fake_home: Path):
        install_wrappers(providers=["claude"])
        zshrc = fake_home / ".zshrc"
        text = zshrc.read_text()
        assert _marker_begin("claude") in text
        assert _marker_begin("codex") not in text

    def test_install_skips_missing_binary(self, fake_home: Path):
        (fake_home / "fakebin" / "codex").unlink()
        results = install_wrappers()
        assert "skipped" in results["codex"]
        assert "installed" in results["claude"]

    def test_install_skips_all_when_no_binaries(self, fake_home: Path):
        (fake_home / "fakebin" / "claude").unlink()
        (fake_home / "fakebin" / "codex").unlink()
        results = install_wrappers()
        assert "skipped" in results["claude"]
        assert "skipped" in results["codex"]

    def test_invalid_provider_raises(self, fake_home: Path):
        with pytest.raises(ValueError, match="Unsupported provider"):
            install_wrappers(providers=["gemini"])

    def test_install_idempotent(self, fake_home: Path):
        install_wrappers()
        install_wrappers()
        zshrc = fake_home / ".zshrc"
        text = zshrc.read_text()
        assert text.count(_marker_begin("claude")) == 1


class TestUninstallWrappers:
    def test_uninstall_removes_functions(self, fake_home: Path):
        install_wrappers()
        results = uninstall_wrappers()
        zshrc = fake_home / ".zshrc"
        text = zshrc.read_text()
        assert _marker_begin("claude") not in text
        assert _marker_begin("codex") not in text
        assert "removed" in results["claude"]
        assert "removed" in results["codex"]

    def test_uninstall_single_provider(self, fake_home: Path):
        install_wrappers()
        uninstall_wrappers(providers=["claude"])
        zshrc = fake_home / ".zshrc"
        text = zshrc.read_text()
        assert _marker_begin("claude") not in text
        # Codex should remain
        assert _marker_begin("codex") in text

    def test_uninstall_when_not_installed(self, fake_home: Path):
        results = uninstall_wrappers()
        assert "not installed" in results["claude"]


class TestGetWrapperStatus:
    def test_status_not_installed(self, fake_home: Path):
        status = get_wrapper_status()
        assert status["claude"]["installed"] is False
        assert status["codex"]["installed"] is False

    def test_status_after_install(self, fake_home: Path):
        install_wrappers()
        status = get_wrapper_status()
        assert status["claude"]["installed"] is True
        assert status["codex"]["installed"] is True

    def test_status_shows_real_binary(self, fake_home: Path):
        status = get_wrapper_status()
        assert "fakebin" in str(status["claude"]["real_binary"])

    def test_status_partial_install(self, fake_home: Path):
        install_wrappers(providers=["claude"])
        status = get_wrapper_status()
        assert status["claude"]["installed"] is True
        assert status["codex"]["installed"] is False


# ------------------------------------------------------------------
# find_real_binary
# ------------------------------------------------------------------


class TestFindRealBinary:
    def test_finds_binary(self, fake_home: Path):
        path = _find_real_binary("claude")
        assert path is not None
        assert "fakebin" in path

    def test_returns_none_for_missing(self, fake_home: Path):
        assert _find_real_binary("nonexistent-tool") is None


# ------------------------------------------------------------------
# Exit code constant
# ------------------------------------------------------------------


class TestExitCode:
    def test_exit_setup_failed_is_78(self):
        assert EXIT_SETUP_FAILED == 78
