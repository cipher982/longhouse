"""Tests for shipper service installation."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.shipper.service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT,
    Platform,
    ServiceConfig,
    _generate_launchd_plist,
    _generate_systemd_unit,
    _get_launchd_plist_path,
    _get_launchd_status,
    _get_systemd_status,
    _get_systemd_unit_path,
    detect_platform,
    get_service_info,
    get_service_status,
    get_zerg_executable,
    install_service,
    uninstall_service,
)


class TestPlatformDetection:
    """Tests for platform detection."""

    def test_detect_macos(self):
        """Detect macOS platform."""
        with patch.object(sys, "platform", "darwin"):
            assert detect_platform() == Platform.MACOS

    def test_detect_linux(self):
        """Detect Linux platform."""
        with patch.object(sys, "platform", "linux"):
            assert detect_platform() == Platform.LINUX

        with patch.object(sys, "platform", "linux2"):
            assert detect_platform() == Platform.LINUX

    def test_detect_unsupported(self):
        """Detect unsupported platform."""
        with patch.object(sys, "platform", "win32"):
            assert detect_platform() == Platform.UNSUPPORTED

        with patch.object(sys, "platform", "freebsd"):
            assert detect_platform() == Platform.UNSUPPORTED


class TestServiceConfig:
    """Tests for ServiceConfig."""

    def test_default_config(self):
        """Default config has expected values."""
        config = ServiceConfig(url="http://localhost:47300")
        assert config.url == "http://localhost:47300"
        assert config.token is None
        assert config.claude_dir is None
        assert config.poll_mode is False
        assert config.interval == 30

    def test_full_config(self):
        """Full config with all options."""
        config = ServiceConfig(
            url="https://api.longhouse.ai",
            token="test-token",
            claude_dir="/custom/claude",
            poll_mode=True,
            interval=60,
        )
        assert config.url == "https://api.longhouse.ai"
        assert config.token == "test-token"
        assert config.claude_dir == "/custom/claude"
        assert config.poll_mode is True
        assert config.interval == 60


class TestZergExecutable:
    """Tests for finding zerg executable."""

    def test_finds_zerg_in_path(self):
        """Find zerg when it's in PATH."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/local/bin/zerg"
            assert get_zerg_executable() == "/usr/local/bin/zerg"

    def test_falls_back_to_uv(self):
        """Fall back to uv run when zerg not in PATH."""
        with patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/local/bin/uv" if cmd == "uv" else None
            result = get_zerg_executable()
            assert "uv" in result
            assert "run" in result
            assert "zerg" in result

    def test_default_longhouse(self):
        """Default to 'longhouse' when nothing found."""
        with patch("shutil.which", return_value=None):
            assert get_zerg_executable() == "longhouse"


class TestServicePaths:
    """Tests for service file paths."""

    def test_launchd_plist_path(self):
        """Launchd plist path is correct."""
        path = _get_launchd_plist_path()
        assert path.parent.name == "LaunchAgents"
        assert path.parent.parent.name == "Library"
        assert path.name == f"{LAUNCHD_LABEL}.plist"

    def test_systemd_unit_path(self):
        """Systemd unit path is correct."""
        path = _get_systemd_unit_path()
        assert path.parent.name == "user"
        assert path.parent.parent.name == "systemd"
        assert path.name == f"{SYSTEMD_UNIT}.service"


class TestLaunchdPlistGeneration:
    """Tests for launchd plist generation."""

    def test_basic_plist(self):
        """Generate basic plist without token."""
        config = ServiceConfig(url="http://localhost:47300")

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/local/bin/zerg"):
            plist = _generate_launchd_plist(config)

        assert '<?xml version="1.0"' in plist
        assert LAUNCHD_LABEL in plist
        assert "/usr/local/bin/zerg" in plist
        assert "connect" in plist
        assert "--url" in plist
        assert "http://localhost:47300" in plist
        assert "RunAtLoad" in plist
        assert "KeepAlive" in plist
        # No token - no env vars section
        assert "AGENTS_API_TOKEN" not in plist

    def test_plist_with_token(self):
        """Generate plist with API token."""
        config = ServiceConfig(
            url="https://api.longhouse.ai",
            token="secret-token-123",
        )

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
            plist = _generate_launchd_plist(config)

        assert "EnvironmentVariables" in plist
        assert "AGENTS_API_TOKEN" in plist
        assert "secret-token-123" in plist

    def test_plist_with_poll_mode(self):
        """Generate plist with polling mode."""
        config = ServiceConfig(
            url="http://localhost:47300",
            poll_mode=True,
            interval=60,
        )

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
            plist = _generate_launchd_plist(config)

        assert "--poll" in plist
        assert "--interval" in plist
        assert "60" in plist

    def test_plist_with_claude_dir(self):
        """Generate plist with custom claude directory."""
        config = ServiceConfig(
            url="http://localhost:47300",
            claude_dir="/custom/claude/path",
        )

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
            plist = _generate_launchd_plist(config)

        assert "--claude-dir" in plist
        assert "/custom/claude/path" in plist

    def test_plist_with_uv_command(self):
        """Generate plist when using uv run."""
        config = ServiceConfig(url="http://localhost:47300")

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/opt/homebrew/bin/uv run zerg"):
            plist = _generate_launchd_plist(config)

        assert "/opt/homebrew/bin/uv" in plist
        assert "<string>run</string>" in plist
        assert "<string>zerg</string>" in plist


class TestSystemdUnitGeneration:
    """Tests for systemd unit generation."""

    def test_basic_unit(self):
        """Generate basic systemd unit without token."""
        config = ServiceConfig(url="http://localhost:47300")

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/local/bin/zerg"):
            unit = _generate_systemd_unit(config)

        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        assert "Description=Longhouse Shipper" in unit
        assert "ExecStart=/usr/local/bin/zerg connect --url http://localhost:47300" in unit
        assert "Restart=on-failure" in unit
        assert "WantedBy=default.target" in unit
        # No token - no Environment line
        assert "AGENTS_API_TOKEN" not in unit

    def test_unit_with_token(self):
        """Generate unit with API token."""
        config = ServiceConfig(
            url="https://api.longhouse.ai",
            token="secret-token-123",
        )

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
            unit = _generate_systemd_unit(config)

        assert 'Environment="AGENTS_API_TOKEN=secret-token-123"' in unit

    def test_unit_with_poll_mode(self):
        """Generate unit with polling mode."""
        config = ServiceConfig(
            url="http://localhost:47300",
            poll_mode=True,
            interval=60,
        )

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
            unit = _generate_systemd_unit(config)

        assert "--poll" in unit
        assert "--interval 60" in unit

    def test_unit_with_claude_dir(self):
        """Generate unit with custom claude directory."""
        config = ServiceConfig(
            url="http://localhost:47300",
            claude_dir="/custom/claude/path",
        )

        with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
            unit = _generate_systemd_unit(config)

        assert "--claude-dir /custom/claude/path" in unit


class TestLaunchdStatus:
    """Tests for launchd status checking."""

    def test_not_installed(self, tmp_path: Path):
        """Status is not-installed when plist doesn't exist."""
        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=tmp_path / "nonexistent.plist"):
            assert _get_launchd_status() == "not-installed"

    def test_running(self, tmp_path: Path):
        """Status is running when launchctl print reports state = running."""
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")

        # launchctl print gui/<uid>/<label> output format
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """com.longhouse.shipper = {
    active count = 1
    path = /Users/user/Library/LaunchAgents/com.longhouse.shipper.plist
    state = running
    pid = 12345
}"""

        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_launchd_status() == "running"

    def test_running_via_pid(self, tmp_path: Path):
        """Status is running when launchctl print has pid but no state field."""
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """com.longhouse.shipper = {
    active count = 1
    pid = 12345
}"""

        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_launchd_status() == "running"

    def test_stopped(self, tmp_path: Path):
        """Status is stopped when launchctl print shows no running state or PID."""
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")

        # launchctl print output when service is loaded but not running
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """com.longhouse.shipper = {
    active count = 0
    path = /Users/user/Library/LaunchAgents/com.longhouse.shipper.plist
    state = waiting
}"""

        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_launchd_status() == "stopped"

    def test_not_loaded(self, tmp_path: Path):
        """Status is stopped when launchctl fails (not loaded)."""
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")

        mock_result = MagicMock()
        mock_result.returncode = 113  # Not found
        mock_result.stdout = ""

        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_launchd_status() == "stopped"


class TestSystemdStatus:
    """Tests for systemd status checking."""

    def test_not_installed(self, tmp_path: Path):
        """Status is not-installed when unit doesn't exist."""
        with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=tmp_path / "nonexistent.service"):
            assert _get_systemd_status() == "not-installed"

    def test_running(self, tmp_path: Path):
        """Status is running when systemctl reports active."""
        unit_path = tmp_path / "test.service"
        unit_path.write_text("[Unit]")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "active"

        with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_systemd_status() == "running"

    def test_stopped(self, tmp_path: Path):
        """Status is stopped when systemctl reports inactive."""
        unit_path = tmp_path / "test.service"
        unit_path.write_text("[Unit]")

        mock_result = MagicMock()
        mock_result.returncode = 3
        mock_result.stdout = "inactive"

        with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_systemd_status() == "stopped"


class TestInstallService:
    """Tests for install_service."""

    def test_unsupported_platform(self):
        """Raises error on unsupported platform."""
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.UNSUPPORTED):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                install_service(url="http://localhost:47300")

    def test_install_macos(self, tmp_path: Path):
        """Install service on macOS."""
        plist_path = tmp_path / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = ""

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", return_value=mock_run):
                    with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
                        result = install_service(
                            url="http://localhost:47300",
                            token="test-token",
                        )

        assert result["success"] is True
        assert result["platform"] == "macos"
        assert result["service"] == LAUNCHD_LABEL
        assert plist_path.exists()
        assert "test-token" in plist_path.read_text()

    def test_install_linux(self, tmp_path: Path):
        """Install service on Linux."""
        unit_path = tmp_path / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"

        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = ""

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.LINUX):
            with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
                with patch("subprocess.run", return_value=mock_run):
                    with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
                        result = install_service(
                            url="http://localhost:47300",
                            token="test-token",
                        )

        assert result["success"] is True
        assert result["platform"] == "linux"
        assert result["service"] == SYSTEMD_UNIT
        assert unit_path.exists()
        assert "test-token" in unit_path.read_text()

    def test_install_macos_load_failure(self, tmp_path: Path):
        """Raises error when launchctl load fails."""
        plist_path = tmp_path / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

        # First call (unload) succeeds, second call (load) fails
        def mock_run_side_effect(*args, **kwargs):
            result = MagicMock()
            if "load" in args[0]:
                result.returncode = 1
                result.stderr = "Load failed"
                result.stdout = ""
            else:
                result.returncode = 0
                result.stderr = ""
                result.stdout = ""
            return result

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", side_effect=mock_run_side_effect):
                    with patch("zerg.services.shipper.service.get_zerg_executable", return_value="/usr/bin/zerg"):
                        with pytest.raises(RuntimeError, match="Failed to load launchd"):
                            install_service(url="http://localhost:47300")


class TestUninstallService:
    """Tests for uninstall_service."""

    def test_unsupported_platform(self):
        """Raises error on unsupported platform."""
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.UNSUPPORTED):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                uninstall_service()

    def test_uninstall_macos_not_installed(self, tmp_path: Path):
        """Uninstall returns success when not installed."""
        plist_path = tmp_path / "nonexistent.plist"

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                result = uninstall_service()

        assert result["success"] is True
        assert "not installed" in result["message"]

    def test_uninstall_macos(self, tmp_path: Path):
        """Uninstall service on macOS."""
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")

        mock_run = MagicMock()
        mock_run.returncode = 0

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", return_value=mock_run):
                    result = uninstall_service()

        assert result["success"] is True
        assert not plist_path.exists()

    def test_uninstall_linux_not_installed(self, tmp_path: Path):
        """Uninstall returns success when not installed."""
        unit_path = tmp_path / "nonexistent.service"

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.LINUX):
            with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
                result = uninstall_service()

        assert result["success"] is True
        assert "not installed" in result["message"]

    def test_uninstall_linux(self, tmp_path: Path):
        """Uninstall service on Linux."""
        unit_path = tmp_path / "test.service"
        unit_path.write_text("[Unit]")

        mock_run = MagicMock()
        mock_run.returncode = 0

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.LINUX):
            with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
                with patch("subprocess.run", return_value=mock_run):
                    result = uninstall_service()

        assert result["success"] is True
        assert not unit_path.exists()


class TestGetServiceStatus:
    """Tests for get_service_status."""

    def test_macos_status(self, tmp_path: Path):
        """Get status on macOS."""
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """com.longhouse.shipper = {
    state = running
    pid = 12345
}"""

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", return_value=mock_result):
                    assert get_service_status() == "running"

    def test_linux_status(self, tmp_path: Path):
        """Get status on Linux."""
        unit_path = tmp_path / "test.service"
        unit_path.write_text("[Unit]")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "active"

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.LINUX):
            with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
                with patch("subprocess.run", return_value=mock_result):
                    assert get_service_status() == "running"

    def test_unsupported_status(self):
        """Get status on unsupported platform."""
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.UNSUPPORTED):
            assert get_service_status() == "not-installed"


class TestGetServiceInfo:
    """Tests for get_service_info."""

    def test_macos_info(self, tmp_path: Path):
        """Get info on macOS."""
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """com.longhouse.shipper = {
    state = running
    pid = 12345
}"""

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", return_value=mock_result):
                    info = get_service_info()

        assert info["platform"] == "macos"
        assert info["status"] == "running"
        assert info["service_name"] == LAUNCHD_LABEL
        assert "shipper.log" in info["log_path"]

    def test_linux_info(self, tmp_path: Path):
        """Get info on Linux."""
        unit_path = tmp_path / "test.service"
        unit_path.write_text("[Unit]")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "active"

        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.LINUX):
            with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
                with patch("subprocess.run", return_value=mock_result):
                    info = get_service_info()

        assert info["platform"] == "linux"
        assert info["status"] == "running"
        assert info["service_name"] == SYSTEMD_UNIT
        assert "shipper.log" in info["log_path"]
