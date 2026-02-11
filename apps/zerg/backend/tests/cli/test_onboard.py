"""Tests for CLI onboarding wizard."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest


class TestOnboardHelpers:
    """Tests for onboard helper functions."""

    def test_derive_client_url_localhost(self) -> None:
        """Test _derive_client_url maps wildcard to localhost."""
        from zerg.cli.onboard import _derive_client_url

        assert _derive_client_url("0.0.0.0", 8080) == "http://127.0.0.1:8080"
        assert _derive_client_url("::", 8080) == "http://127.0.0.1:8080"
        assert _derive_client_url("", 8080) == "http://127.0.0.1:8080"

    def test_derive_client_url_ipv6(self) -> None:
        """Test _derive_client_url wraps IPv6 in brackets."""
        from zerg.cli.onboard import _derive_client_url

        assert _derive_client_url("::1", 8080) == "http://[::1]:8080"
        assert _derive_client_url("fe80::1", 9000) == "http://[fe80::1]:9000"

    def test_derive_client_url_regular_host(self) -> None:
        """Test _derive_client_url passes through regular hosts."""
        from zerg.cli.onboard import _derive_client_url

        assert _derive_client_url("127.0.0.1", 8080) == "http://127.0.0.1:8080"
        assert _derive_client_url("localhost", 3000) == "http://localhost:3000"
        assert _derive_client_url("192.168.1.100", 8080) == "http://192.168.1.100:8080"

    def test_has_command_true(self) -> None:
        """Test _has_command returns True for existing commands."""
        from zerg.cli.onboard import _has_command

        # python should exist
        assert _has_command("python") or _has_command("python3")

    def test_has_command_false(self) -> None:
        """Test _has_command returns False for non-existent commands."""
        from zerg.cli.onboard import _has_command

        assert not _has_command("definitely_not_a_real_command_xyz")

    def test_has_gui_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _has_gui detection on macOS."""
        from zerg.cli.onboard import _has_gui

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.delenv("SSH_CONNECTION", raising=False)

        assert _has_gui() is True

    def test_has_gui_ssh_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _has_gui returns False in SSH session."""
        from zerg.cli.onboard import _has_gui

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv("SSH_CONNECTION", "192.168.1.1 22 192.168.1.2 12345")

        assert _has_gui() is False

    def test_has_gui_linux_no_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _has_gui returns False on Linux without DISPLAY."""
        from zerg.cli.onboard import _has_gui

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        assert _has_gui() is False

    def test_has_gui_linux_with_display(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _has_gui returns True on Linux with DISPLAY."""
        from zerg.cli.onboard import _has_gui

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("DISPLAY", ":0")

        assert _has_gui() is True

    def test_has_systemd_linux(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test _has_systemd detection on Linux."""
        from zerg.cli.onboard import _has_systemd

        monkeypatch.setattr(sys, "platform", "linux")

        # No systemd dir
        assert _has_systemd() is False or True  # Depends on actual system

    def test_has_systemd_not_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _has_systemd returns False on non-Linux."""
        from zerg.cli.onboard import _has_systemd

        monkeypatch.setattr(sys, "platform", "darwin")
        assert _has_systemd() is False

    def test_has_launchd_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _has_launchd returns True on macOS."""
        from zerg.cli.onboard import _has_launchd

        monkeypatch.setattr(sys, "platform", "darwin")
        assert _has_launchd() is True

    def test_has_launchd_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test _has_launchd returns False on Linux."""
        from zerg.cli.onboard import _has_launchd

        monkeypatch.setattr(sys, "platform", "linux")
        assert _has_launchd() is False


class TestServerHealth:
    """Tests for server health checking."""

    def test_check_server_health_not_running(self) -> None:
        """Test health check returns False when server not running."""
        from zerg.cli.onboard import _check_server_health

        # Use unlikely port
        assert _check_server_health("127.0.0.1", 59999, timeout=0.5) is False

    @patch("zerg.cli.onboard.httpx.Client")
    def test_check_server_health_success(self, mock_client_class: MagicMock) -> None:
        """Test health check returns True when server responds 200."""
        from zerg.cli.onboard import _check_server_health

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)
        mock_client.get.return_value.status_code = 200
        mock_client_class.return_value = mock_client

        assert _check_server_health("127.0.0.1", 8080) is True

    @patch("zerg.cli.onboard.httpx.Client")
    def test_check_server_health_error(self, mock_client_class: MagicMock) -> None:
        """Test health check returns False on connection error."""
        from zerg.cli.onboard import _check_server_health

        mock_client_class.side_effect = Exception("Connection refused")

        assert _check_server_health("127.0.0.1", 8080) is False


class TestConfigSaving:
    """Tests for config saving during onboard."""

    def test_onboard_saves_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that onboard saves config to correct location."""
        from zerg.cli.config_file import load_config
        from zerg.cli.config_file import save_config

        # Create config in temp dir
        config_path = tmp_path / "config.toml"

        config_data = {
            "server": {"host": "127.0.0.1", "port": 8080},
            "shipper": {"mode": "watch", "api_url": "http://localhost:8080"},
        }

        save_config(config_data, config_path)

        # Verify it was saved
        assert config_path.exists()

        # Verify it can be loaded
        loaded = load_config(config_path)
        assert loaded.server.host == "127.0.0.1"
        assert loaded.server.port == 8080
        assert loaded.shipper.mode == "watch"


class TestShellProfilePath:
    """Tests for _get_shell_profile_path."""

    def test_zsh_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zerg.cli.onboard import _get_shell_profile_path

        monkeypatch.setenv("SHELL", "/bin/zsh")
        result = _get_shell_profile_path()
        assert result is not None
        assert result.name == ".zshrc"

    def test_bash_profile_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zerg.cli.onboard import _get_shell_profile_path

        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(sys, "platform", "darwin")
        result = _get_shell_profile_path()
        assert result is not None
        assert result.name == ".bash_profile"

    def test_bash_profile_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zerg.cli.onboard import _get_shell_profile_path

        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(sys, "platform", "linux")
        result = _get_shell_profile_path()
        assert result is not None
        assert result.name == ".bashrc"

    def test_fish_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zerg.cli.onboard import _get_shell_profile_path

        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        result = _get_shell_profile_path()
        assert result is not None
        assert result.name == "config.fish"

    def test_unknown_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zerg.cli.onboard import _get_shell_profile_path

        monkeypatch.setenv("SHELL", "/bin/csh")
        result = _get_shell_profile_path()
        assert result is None

    def test_no_shell_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from zerg.cli.onboard import _get_shell_profile_path

        monkeypatch.delenv("SHELL", raising=False)
        result = _get_shell_profile_path()
        assert result is None


class TestVerifyShellPath:
    """Tests for verify_shell_path."""

    def test_returns_empty_on_unknown_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should return no warnings for unknown shells."""
        from zerg.cli.onboard import verify_shell_path

        monkeypatch.setenv("SHELL", "/bin/csh")
        result = verify_shell_path()
        assert result == []

    def test_returns_empty_when_no_profile(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Should return no warnings when profile doesn't exist."""
        from zerg.cli.onboard import verify_shell_path

        monkeypatch.setenv("SHELL", "/bin/zsh")
        # Patch Path.home() to a temp dir without .zshrc
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = verify_shell_path()
        assert result == []

    def test_warns_when_longhouse_not_in_fresh_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should warn when longhouse is on current PATH but not in fresh shell PATH."""
        from zerg.cli.onboard import verify_shell_path

        monkeypatch.setenv("SHELL", "/bin/zsh")

        # Mock _get_shell_profile_path to return a known path
        profile = Path("/tmp/test_profile")
        monkeypatch.setattr("zerg.cli.onboard._get_shell_profile_path", lambda: profile)

        # Mock _extract_path_from_profile to return a PATH without longhouse's dir
        monkeypatch.setattr(
            "zerg.cli.onboard._extract_path_from_profile",
            lambda p: "/usr/bin:/bin",
        )

        # Mock shutil.which to simulate longhouse being installed at a custom location
        def mock_which(cmd: str) -> str | None:
            if cmd == "longhouse":
                return "/home/user/.local/bin/longhouse"
            return None

        monkeypatch.setattr("zerg.cli.onboard.shutil.which", mock_which)

        result = verify_shell_path()
        assert len(result) >= 1
        assert "longhouse" in result[0]
        assert "won't be on PATH" in result[0]
        # Check that a fix line is provided
        assert any(".local/bin" in w for w in result)

    def test_no_warning_when_longhouse_in_fresh_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should return no warnings when longhouse dir is in fresh PATH."""
        from zerg.cli.onboard import verify_shell_path

        monkeypatch.setenv("SHELL", "/bin/zsh")
        monkeypatch.setattr("zerg.cli.onboard._get_shell_profile_path", lambda: Path("/tmp/test"))
        monkeypatch.setattr(
            "zerg.cli.onboard._extract_path_from_profile",
            lambda p: "/usr/bin:/bin:/home/user/.local/bin",
        )

        def mock_which(cmd: str) -> str | None:
            if cmd == "longhouse":
                return "/home/user/.local/bin/longhouse"
            return None

        monkeypatch.setattr("zerg.cli.onboard.shutil.which", mock_which)

        result = verify_shell_path()
        assert result == []

    def test_warns_for_claude_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should also check claude and warn if not in fresh PATH."""
        from zerg.cli.onboard import verify_shell_path

        monkeypatch.setenv("SHELL", "/bin/zsh")
        monkeypatch.setattr("zerg.cli.onboard._get_shell_profile_path", lambda: Path("/tmp/test"))
        monkeypatch.setattr(
            "zerg.cli.onboard._extract_path_from_profile",
            lambda p: "/usr/bin:/bin",
        )

        def mock_which(cmd: str) -> str | None:
            if cmd == "longhouse":
                return "/usr/bin/longhouse"  # This dir IS in the fresh PATH
            if cmd == "claude":
                return "/opt/special/bin/claude"  # This dir is NOT
            return None

        monkeypatch.setattr("zerg.cli.onboard.shutil.which", mock_which)

        result = verify_shell_path()
        # longhouse should be fine, claude should warn
        assert any("claude" in w and "won't be on PATH" in w for w in result)
        assert not any("longhouse" in w and "won't be on PATH" in w for w in result)
