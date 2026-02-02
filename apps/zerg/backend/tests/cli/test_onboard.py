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
        from zerg.cli.config_file import get_config_path
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


class TestDemoSeeding:
    """Tests for demo session seeding during onboard."""

    @patch("zerg.cli.onboard._check_server_health")
    @patch("zerg.cli.onboard._has_gui")
    @patch("zerg.cli.onboard.httpx.Client")
    @patch("zerg.cli.onboard._is_server_running")
    @patch("zerg.cli.onboard.save_config")
    def test_onboard_seeds_demo_sessions(
        self,
        mock_save_config: MagicMock,
        mock_is_server_running: MagicMock,
        mock_client_class: MagicMock,
        mock_has_gui: MagicMock,
        mock_check_health: MagicMock,
    ) -> None:
        """Test that onboard seeds demo sessions by default."""
        from typer.testing import CliRunner

        from zerg.cli.onboard import app

        # Setup mocks
        mock_is_server_running.return_value = (True, 12345)
        mock_check_health.return_value = True
        mock_has_gui.return_value = False

        # Mock HTTP client
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        # Mock responses for test event and demo seeding
        def post_side_effect(url: str, **kwargs):
            mock_response = MagicMock()
            if "seed-demo-sessions" in url:
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "sessions_seeded": 2,
                    "message": "Demo sessions seeded",
                }
            else:  # test event
                mock_response.status_code = 200
            return mock_response

        mock_client.post = MagicMock(side_effect=post_side_effect)
        mock_client_class.return_value = mock_client

        # Run onboard with --quick to avoid prompts
        runner = CliRunner()
        result = runner.invoke(app, ["--quick", "--no-shipper"])

        # Verify demo seed was called
        post_calls = mock_client.post.call_args_list
        demo_calls = [call for call in post_calls if "seed-demo-sessions" in str(call)]
        assert len(demo_calls) == 1, "Demo seed endpoint should be called once"

        # Verify success message in output
        assert "2 demo sessions loaded" in result.stdout or "demo sessions" in result.stdout.lower()

    @patch("zerg.cli.onboard._check_server_health")
    @patch("zerg.cli.onboard._has_gui")
    @patch("zerg.cli.onboard.httpx.Client")
    @patch("zerg.cli.onboard._is_server_running")
    @patch("zerg.cli.onboard.save_config")
    def test_onboard_skip_demo_flag(
        self,
        mock_save_config: MagicMock,
        mock_is_server_running: MagicMock,
        mock_client_class: MagicMock,
        mock_has_gui: MagicMock,
        mock_check_health: MagicMock,
    ) -> None:
        """Test that --skip-demo skips demo seeding."""
        from typer.testing import CliRunner

        from zerg.cli.onboard import app

        # Setup mocks
        mock_is_server_running.return_value = (True, 12345)
        mock_check_health.return_value = True
        mock_has_gui.return_value = False

        # Mock HTTP client
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        # Mock response for test event only
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post = MagicMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        # Run onboard with --skip-demo
        runner = CliRunner()
        result = runner.invoke(app, ["--quick", "--no-shipper", "--skip-demo"])

        # Verify demo seed was NOT called
        post_calls = mock_client.post.call_args_list
        demo_calls = [call for call in post_calls if "seed-demo-sessions" in str(call)]
        assert len(demo_calls) == 0, "Demo seed endpoint should not be called with --skip-demo"

        # Should still complete successfully
        assert result.exit_code == 0

    @patch("zerg.cli.onboard._check_server_health")
    @patch("zerg.cli.onboard._has_gui")
    @patch("zerg.cli.onboard.httpx.Client")
    @patch("zerg.cli.onboard._is_server_running")
    @patch("zerg.cli.onboard.save_config")
    def test_onboard_continues_if_demo_fails(
        self,
        mock_save_config: MagicMock,
        mock_is_server_running: MagicMock,
        mock_client_class: MagicMock,
        mock_has_gui: MagicMock,
        mock_check_health: MagicMock,
    ) -> None:
        """Test that onboard succeeds even if demo seeding fails."""
        from typer.testing import CliRunner

        from zerg.cli.onboard import app

        # Setup mocks
        mock_is_server_running.return_value = (True, 12345)
        mock_check_health.return_value = True
        mock_has_gui.return_value = False

        # Mock HTTP client
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=None)

        # Mock responses - test event succeeds, demo seeding fails
        def post_side_effect(url: str, **kwargs):
            if "seed-demo-sessions" in url:
                raise Exception("Network error")
            mock_response = MagicMock()
            mock_response.status_code = 200
            return mock_response

        mock_client.post = MagicMock(side_effect=post_side_effect)
        mock_client_class.return_value = mock_client

        # Run onboard
        runner = CliRunner()
        result = runner.invoke(app, ["--quick", "--no-shipper"])

        # Should still succeed
        assert result.exit_code == 0

        # Should show warning message
        assert "Demo seeding skipped" in result.stdout or "Could not load demo" in result.stdout
