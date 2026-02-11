"""Tests for shipper token storage."""

from pathlib import Path

import pytest

from zerg.services.shipper.token import clear_token
from zerg.services.shipper.token import get_token_path
from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import load_token
from zerg.services.shipper.token import save_token
from zerg.services.shipper.token import save_zerg_url


class TestTokenPath:
    """Tests for get_token_path."""

    def test_default_path(self, tmp_path: Path, monkeypatch):
        """Uses ~/.claude by default."""
        # Clear CLAUDE_CONFIG_DIR env var
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        # Mock home to tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        path = get_token_path()

        assert path == tmp_path / ".claude" / "longhouse-device-token"

    def test_explicit_config_dir(self, tmp_path: Path):
        """Uses explicit config dir when provided."""
        config_dir = tmp_path / "custom-claude"
        config_dir.mkdir()

        path = get_token_path(config_dir)

        assert path == config_dir / "longhouse-device-token"

    def test_env_var_config_dir(self, tmp_path: Path, monkeypatch):
        """Uses CLAUDE_CONFIG_DIR env var."""
        config_dir = tmp_path / "env-claude"
        config_dir.mkdir()

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        path = get_token_path()

        assert path == config_dir / "longhouse-device-token"


class TestTokenStorage:
    """Tests for save_token, load_token, clear_token."""

    def test_save_and_load(self, tmp_path: Path):
        """Save and load a token."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()

        save_token("zdt_test_token_123", config_dir)
        loaded = load_token(config_dir)

        assert loaded == "zdt_test_token_123"

    def test_load_nonexistent(self, tmp_path: Path):
        """Load returns None when no token exists."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()

        loaded = load_token(config_dir)

        assert loaded is None

    def test_load_empty_file(self, tmp_path: Path):
        """Load returns None for empty file."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()
        token_path = config_dir / "longhouse-device-token"
        token_path.write_text("")

        loaded = load_token(config_dir)

        assert loaded is None

    def test_clear_existing(self, tmp_path: Path):
        """Clear removes existing token."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()

        save_token("zdt_test_token", config_dir)
        result = clear_token(config_dir)

        assert result is True
        assert load_token(config_dir) is None

    def test_clear_nonexistent(self, tmp_path: Path):
        """Clear returns False when no token exists."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()

        result = clear_token(config_dir)

        assert result is False

    def test_save_creates_directory(self, tmp_path: Path):
        """Save creates parent directory if needed."""
        config_dir = tmp_path / "nested" / "claude"

        save_token("zdt_test_token", config_dir)

        assert config_dir.exists()
        assert load_token(config_dir) == "zdt_test_token"


class TestUrlStorage:
    """Tests for save_zerg_url and get_zerg_url."""

    def test_save_and_load_url(self, tmp_path: Path):
        """Save and load a URL."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()

        save_zerg_url("https://api.longhouse.ai", config_dir)
        loaded = get_zerg_url(config_dir)

        assert loaded == "https://api.longhouse.ai"

    def test_load_url_nonexistent(self, tmp_path: Path):
        """Load returns None when no URL exists."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()

        loaded = get_zerg_url(config_dir)

        assert loaded is None

    def test_url_strips_whitespace(self, tmp_path: Path):
        """URL is stripped of whitespace."""
        config_dir = tmp_path / "claude"
        config_dir.mkdir()

        save_zerg_url("  https://api.longhouse.ai  ", config_dir)
        loaded = get_zerg_url(config_dir)

        assert loaded == "https://api.longhouse.ai"
