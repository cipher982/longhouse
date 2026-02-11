"""Tests for CLI config file support."""

from pathlib import Path

import pytest


class TestConfigFile:
    """Tests for config file loading and saving."""

    def test_load_default_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config returns defaults when no file exists."""
        from zerg.cli.config_file import LonghouseConfig
        from zerg.cli.config_file import load_config

        # Point to non-existent config
        fake_config = tmp_path / "config.toml"
        config = load_config(fake_config)

        assert isinstance(config, LonghouseConfig)
        assert config.server.host == "127.0.0.1"
        assert config.server.port == 8080
        assert config.shipper.mode == "watch"
        assert config.shipper.api_url == "http://localhost:8080"

    def test_load_config_from_file(self, tmp_path: Path) -> None:
        """Test loading config from TOML file."""
        from zerg.cli.config_file import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[server]
host = "0.0.0.0"
port = 9000

[shipper]
mode = "poll"
api_url = "https://api.longhouse.ai"
interval = 60
""")

        config = load_config(config_file)

        assert config.server.host == "0.0.0.0"
        assert config.server.port == 9000
        assert config.shipper.mode == "poll"
        assert config.shipper.api_url == "https://api.longhouse.ai"
        assert config.shipper.interval == 60

    def test_env_vars_override_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override file config."""
        from zerg.cli.config_file import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[server]
host = "0.0.0.0"
port = 9000
""")

        # Set env vars
        monkeypatch.setenv("LONGHOUSE_HOST", "192.168.1.100")
        monkeypatch.setenv("LONGHOUSE_PORT", "8888")

        config = load_config(config_file)

        # Env vars should override
        assert config.server.host == "192.168.1.100"
        assert config.server.port == 8888
        assert config._sources["server.host"] == "env"
        assert config._sources["server.port"] == "env"

    def test_longhouse_api_url_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test LONGHOUSE_API_URL is read from environment."""
        from zerg.cli.config_file import load_config

        config_file = tmp_path / "config.toml"

        monkeypatch.setenv("LONGHOUSE_API_URL", "https://api.longhouse.ai")

        config = load_config(config_file)

        assert config.shipper.api_url == "https://api.longhouse.ai"

    def test_save_config_creates_dir(self, tmp_path: Path) -> None:
        """Test save_config creates parent directory if needed."""
        from zerg.cli.config_file import save_config

        config_path = tmp_path / "subdir" / "config.toml"

        save_config(
            {
                "server": {"host": "localhost", "port": 8080},
                "shipper": {"mode": "watch"},
            },
            config_path,
        )

        assert config_path.exists()
        content = config_path.read_text()
        assert "localhost" in content
        assert "8080" in content

    def test_save_config_permissions(self, tmp_path: Path) -> None:
        """Test save_config sets secure file permissions."""
        from zerg.cli.config_file import save_config

        config_path = tmp_path / "config.toml"

        save_config({"server": {"host": "localhost"}}, config_path)

        # Check permissions (600 = owner read/write only)
        mode = config_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_get_effective_config_display(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test effective config display shows sources correctly."""
        from zerg.cli.config_file import get_effective_config_display
        from zerg.cli.config_file import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[server]
host = "filehost"
""")

        monkeypatch.setenv("LONGHOUSE_PORT", "9999")

        config = load_config(config_file)
        entries = get_effective_config_display(config)

        # Convert to dict for easier checking
        entry_dict = {key: (value, source) for key, value, source in entries}

        assert entry_dict["server.host"] == ("filehost", "file")
        assert entry_dict["server.port"] == ("9999", "env")
        assert entry_dict["shipper.mode"][1] == "default"

    def test_malformed_config_file(self, tmp_path: Path) -> None:
        """Test graceful handling of malformed config file."""
        from zerg.cli.config_file import load_config

        config_file = tmp_path / "config.toml"
        config_file.write_text("this is not valid toml [[[")

        # Should not raise, should return defaults
        config = load_config(config_file)
        assert config.server.host == "127.0.0.1"
