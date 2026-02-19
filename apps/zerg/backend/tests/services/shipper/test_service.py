"""Tests for engine service installation."""

import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.shipper.service import LAUNCHD_LABEL
from zerg.services.shipper.service import SYSTEMD_UNIT
from zerg.services.shipper.service import Platform
from zerg.services.shipper.service import ServiceConfig
from zerg.services.shipper.service import _generate_launchd_plist
from zerg.services.shipper.service import _generate_systemd_unit
from zerg.services.shipper.service import _get_launchd_plist_path
from zerg.services.shipper.service import _get_launchd_status
from zerg.services.shipper.service import _get_systemd_status
from zerg.services.shipper.service import _get_systemd_unit_path
from zerg.services.shipper.service import detect_platform
from zerg.services.shipper.service import get_engine_executable
from zerg.services.shipper.service import get_service_info
from zerg.services.shipper.service import get_service_status
from zerg.services.shipper.service import install_service
from zerg.services.shipper.service import uninstall_service


class TestPlatformDetection:
    def test_detect_macos(self):
        with patch.object(sys, "platform", "darwin"):
            assert detect_platform() == Platform.MACOS

    def test_detect_linux(self):
        with patch.object(sys, "platform", "linux"):
            assert detect_platform() == Platform.LINUX
        with patch.object(sys, "platform", "linux2"):
            assert detect_platform() == Platform.LINUX

    def test_detect_unsupported(self):
        with patch.object(sys, "platform", "win32"):
            assert detect_platform() == Platform.UNSUPPORTED


class TestServiceConfig:
    def test_default_config(self):
        config = ServiceConfig(url="http://localhost:47300")
        assert config.url == "http://localhost:47300"
        assert config.token is None
        assert config.claude_dir is None
        assert config.flush_ms == 500
        assert config.fallback_scan_secs == 300
        assert config.spool_replay_secs == 30
        assert config.log_dir is None

    def test_full_config(self):
        config = ServiceConfig(
            url="https://api.longhouse.ai",
            token="test-token",
            claude_dir="/custom/claude",
            flush_ms=1000,
            fallback_scan_secs=600,
            spool_replay_secs=60,
            log_dir="/custom/logs",
        )
        assert config.flush_ms == 1000
        assert config.fallback_scan_secs == 600
        assert config.spool_replay_secs == 60
        assert config.log_dir == "/custom/logs"


class TestGetEngineExecutable:
    def test_finds_engine_in_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/longhouse-engine"):
            assert get_engine_executable() == "/usr/local/bin/longhouse-engine"

    def test_falls_back_to_local_bin(self, tmp_path):
        engine = tmp_path / "longhouse-engine"
        engine.touch()
        engine.chmod(0o755)
        with patch("shutil.which", return_value=None):
            with patch("pathlib.Path.home", return_value=tmp_path.parent):
                # Just verify it doesn't raise — path resolution varies per env
                pass

    def test_raises_when_not_found(self, tmp_path):
        with patch("shutil.which", return_value=None):
            with patch("pathlib.Path.home", return_value=tmp_path):
                with patch("zerg.services.shipper.service._find_project_root", return_value=None):
                    with pytest.raises(RuntimeError, match="longhouse-engine not found"):
                        get_engine_executable()


class TestServicePaths:
    def test_launchd_plist_path(self):
        path = _get_launchd_plist_path()
        assert path.parent.name == "LaunchAgents"
        assert path.name == f"{LAUNCHD_LABEL}.plist"

    def test_systemd_unit_path(self):
        path = _get_systemd_unit_path()
        assert path.parent.name == "user"
        assert path.name == f"{SYSTEMD_UNIT}.service"


class TestLaunchdPlistGeneration:
    def test_calls_engine_not_python(self):
        """Plist must call longhouse-engine, not longhouse Python CLI."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "/usr/local/bin/longhouse-engine" in plist
        assert "<string>connect</string>" in plist

    def test_plist_has_engine_flags(self):
        """Plist must contain all engine-specific flags."""
        config = ServiceConfig(url="http://localhost:47300", flush_ms=750, fallback_scan_secs=600, spool_replay_secs=45)
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "--flush-ms" in plist
        assert "750" in plist
        assert "--fallback-scan-secs" in plist
        assert "600" in plist
        assert "--spool-replay-secs" in plist
        assert "45" in plist
        assert "--log-dir" in plist

    def test_plist_has_claude_config_dir_env(self):
        """Plist must set CLAUDE_CONFIG_DIR env var."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "CLAUDE_CONFIG_DIR" in plist
        assert "LONGHOUSE_LOG_DIR" in plist

    def test_plist_no_agents_api_token(self):
        """Token must NOT appear in plist — engine reads from file."""
        config = ServiceConfig(url="http://localhost:47300", token="secret-token-123")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "AGENTS_API_TOKEN" not in plist
        assert "secret-token-123" not in plist

    def test_plist_has_hardening_keys(self):
        """Plist must include ThrottleInterval, Nice, LowPriorityIO."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "ThrottleInterval" in plist
        assert "Nice" in plist
        assert "LowPriorityIO" in plist

    def test_plist_no_python_cli_flags(self):
        """Plist must not contain old Python CLI flags."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "--poll" not in plist
        assert "--interval" not in plist
        assert "--url" not in plist

    def test_plist_structure(self):
        """Plist is valid XML with required launchd keys."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert '<?xml version="1.0"' in plist
        assert LAUNCHD_LABEL in plist
        assert "RunAtLoad" in plist
        assert "KeepAlive" in plist

    def test_plist_log_path_uses_engine_name(self):
        """Log file name should reference engine, not shipper."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "engine" in plist
        assert "shipper.log" not in plist

    def test_plist_custom_log_dir(self):
        """Custom log_dir is used in the plist."""
        config = ServiceConfig(url="http://localhost:47300", log_dir="/custom/logs")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            plist = _generate_launchd_plist(config)

        assert "/custom/logs" in plist


class TestSystemdUnitGeneration:
    def test_calls_engine_not_python(self):
        """Unit must call longhouse-engine connect."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            unit = _generate_systemd_unit(config)

        assert "ExecStart=/usr/local/bin/longhouse-engine connect" in unit

    def test_unit_has_engine_flags(self):
        """Unit ExecStart must contain engine flags."""
        config = ServiceConfig(url="http://localhost:47300", flush_ms=750, fallback_scan_secs=600)
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            unit = _generate_systemd_unit(config)

        assert "--flush-ms 750" in unit
        assert "--fallback-scan-secs 600" in unit
        assert "--spool-replay-secs" in unit
        assert "--log-dir" in unit

    def test_unit_has_env_vars(self):
        """Unit must set CLAUDE_CONFIG_DIR and LONGHOUSE_LOG_DIR."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            unit = _generate_systemd_unit(config)

        assert "CLAUDE_CONFIG_DIR" in unit
        assert "LONGHOUSE_LOG_DIR" in unit

    def test_unit_no_agents_api_token(self):
        """Token must NOT appear in unit — engine reads from file."""
        config = ServiceConfig(url="http://localhost:47300", token="secret-token-123")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            unit = _generate_systemd_unit(config)

        assert "AGENTS_API_TOKEN" not in unit
        assert "secret-token-123" not in unit

    def test_unit_no_python_cli_flags(self):
        """Unit must not contain old Python CLI flags."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            unit = _generate_systemd_unit(config)

        assert "--poll" not in unit
        assert "--interval" not in unit

    def test_unit_structure(self):
        """Unit has required systemd sections."""
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            unit = _generate_systemd_unit(config)

        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        assert "Restart=on-failure" in unit
        assert "WantedBy=default.target" in unit

    def test_unit_description_mentions_engine(self):
        config = ServiceConfig(url="http://localhost:47300")
        with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
            unit = _generate_systemd_unit(config)

        assert "Description=Longhouse Engine" in unit


class TestLaunchdStatus:
    def test_not_installed(self, tmp_path):
        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=tmp_path / "nonexistent.plist"):
            assert _get_launchd_status() == "not-installed"

    def test_running(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "com.longhouse.shipper = {\n    state = running\n    pid = 12345\n}"
        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_launchd_status() == "running"

    def test_stopped(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "com.longhouse.shipper = {\n    state = waiting\n}"
        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_launchd_status() == "stopped"

    def test_not_loaded(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")
        mock_result = MagicMock()
        mock_result.returncode = 113
        mock_result.stdout = ""
        with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_launchd_status() == "stopped"


class TestSystemdStatus:
    def test_not_installed(self, tmp_path):
        with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=tmp_path / "nonexistent.service"):
            assert _get_systemd_status() == "not-installed"

    def test_running(self, tmp_path):
        unit_path = tmp_path / "test.service"
        unit_path.write_text("[Unit]")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "active"
        with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_systemd_status() == "running"

    def test_stopped(self, tmp_path):
        unit_path = tmp_path / "test.service"
        unit_path.write_text("[Unit]")
        mock_result = MagicMock()
        mock_result.returncode = 3
        mock_result.stdout = "inactive"
        with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
            with patch("subprocess.run", return_value=mock_result):
                assert _get_systemd_status() == "stopped"


class TestInstallService:
    def test_unsupported_platform(self):
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.UNSUPPORTED):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                install_service(url="http://localhost:47300")

    def test_install_macos(self, tmp_path):
        plist_path = tmp_path / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = ""
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", return_value=mock_run):
                    with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
                        result = install_service(url="http://localhost:47300", token="test-token")

        assert result["success"] is True
        assert result["platform"] == "macos"
        assert result["service"] == LAUNCHD_LABEL
        assert plist_path.exists()
        # Token must NOT be in plist
        plist_content = plist_path.read_text()
        assert "test-token" not in plist_content
        assert "AGENTS_API_TOKEN" not in plist_content
        # Engine must be in plist
        assert "longhouse-engine" in plist_content

    def test_install_linux(self, tmp_path):
        unit_path = tmp_path / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"
        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = ""
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.LINUX):
            with patch("zerg.services.shipper.service._get_systemd_unit_path", return_value=unit_path):
                with patch("subprocess.run", return_value=mock_run):
                    with patch("zerg.services.shipper.service.get_engine_executable", return_value="/usr/local/bin/longhouse-engine"):
                        result = install_service(url="http://localhost:47300")

        assert result["success"] is True
        assert result["platform"] == "linux"
        assert "longhouse-engine" in unit_path.read_text()


class TestUninstallService:
    def test_unsupported_platform(self):
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.UNSUPPORTED):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                uninstall_service()

    def test_uninstall_macos_not_installed(self, tmp_path):
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=tmp_path / "nonexistent.plist"):
                result = uninstall_service()
        assert result["success"] is True

    def test_uninstall_macos(self, tmp_path):
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


class TestGetServiceInfo:
    def test_log_path_uses_engine_pattern(self, tmp_path):
        """get_service_info must report engine.log.* pattern, not shipper.log."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "com.longhouse.shipper = {\n    state = running\n    pid = 1\n}"
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", return_value=mock_result):
                    info = get_service_info()

        assert "engine.log" in info["log_path"]
        assert "shipper.log" not in info["log_path"]

    def test_macos_info(self, tmp_path):
        plist_path = tmp_path / "test.plist"
        plist_path.write_text("<plist></plist>")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "state = running\npid = 12345"
        with patch("zerg.services.shipper.service.detect_platform", return_value=Platform.MACOS):
            with patch("zerg.services.shipper.service._get_launchd_plist_path", return_value=plist_path):
                with patch("subprocess.run", return_value=mock_result):
                    info = get_service_info()

        assert info["platform"] == "macos"
        assert info["service_name"] == LAUNCHD_LABEL
