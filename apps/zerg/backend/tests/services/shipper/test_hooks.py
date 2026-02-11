"""Tests for hook installation and MCP server registration (hooks.py)."""

import json
from pathlib import Path

import pytest
import tomllib

from zerg.services.shipper.hooks import install_codex_mcp_server
from zerg.services.shipper.hooks import install_mcp_server


class TestInstallMcpServer:
    """Tests for Claude Code MCP server registration (~/.claude.json)."""

    def test_creates_claude_json(self, tmp_path: Path) -> None:
        """Creates claude.json with MCP config from scratch."""
        actions = install_mcp_server(claude_dir=str(tmp_path))
        assert len(actions) == 1
        assert "mcpServers.longhouse" in actions[0]

        claude_json = tmp_path / "claude.json"
        config = json.loads(claude_json.read_text())
        assert config["mcpServers"]["longhouse"]["command"] == "longhouse"
        assert config["mcpServers"]["longhouse"]["args"] == ["mcp-server"]

    def test_preserves_existing_config(self, tmp_path: Path) -> None:
        """Existing config is preserved when adding MCP server."""
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(json.dumps({"userTheme": "dark"}) + "\n")

        install_mcp_server(claude_dir=str(tmp_path))

        config = json.loads(claude_json.read_text())
        assert config["userTheme"] == "dark"
        assert "longhouse" in config["mcpServers"]

    def test_idempotent(self, tmp_path: Path) -> None:
        """Running twice produces same result."""
        install_mcp_server(claude_dir=str(tmp_path))
        install_mcp_server(claude_dir=str(tmp_path))

        claude_json = tmp_path / "claude.json"
        config = json.loads(claude_json.read_text())
        assert config["mcpServers"]["longhouse"]["command"] == "longhouse"


class TestInstallCodexMcpServer:
    """Tests for Codex CLI MCP server registration (~/.codex/config.toml)."""

    def test_creates_config_toml(self, tmp_path: Path) -> None:
        """Creates config.toml with MCP config from scratch."""
        actions = install_codex_mcp_server(codex_dir=str(tmp_path))
        assert len(actions) == 1
        assert "[mcp_servers.longhouse]" in actions[0]

        config_path = tmp_path / "config.toml"
        config = tomllib.loads(config_path.read_text())
        lh = config["mcp_servers"]["longhouse"]
        assert lh["command"] == "longhouse"
        assert lh["args"] == ["mcp-server"]

    def test_preserves_existing_config(self, tmp_path: Path) -> None:
        """Existing config is preserved when adding MCP server."""
        config_path = tmp_path / "config.toml"
        existing = (
            'model = "gpt-5.2-codex"\n'
            'auth_method = "api-key"\n'
            "\n"
            '[projects."/Users/test/git/project"]\n'
            'trust_level = "trusted"\n'
            "\n"
            "[mcp_servers.context7]\n"
            'command = "npx"\n'
            'args = ["-y", "@upstash/context7-mcp"]\n'
        )
        config_path.write_text(existing)

        install_codex_mcp_server(codex_dir=str(tmp_path))

        config = tomllib.loads(config_path.read_text())
        assert config["model"] == "gpt-5.2-codex"
        assert config["auth_method"] == "api-key"
        assert "context7" in config["mcp_servers"]
        assert "longhouse" in config["mcp_servers"]

    def test_replaces_existing_longhouse_section(self, tmp_path: Path) -> None:
        """Existing [mcp_servers.longhouse] section is replaced."""
        config_path = tmp_path / "config.toml"
        stale = (
            'model = "gpt-5"\n'
            "\n"
            "[mcp_servers.longhouse]\n"
            'command = "old-binary"\n'
            'args = ["old-arg"]\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "other"\n'
        )
        config_path.write_text(stale)

        install_codex_mcp_server(codex_dir=str(tmp_path))

        text = config_path.read_text()
        assert "old-binary" not in text
        assert "old-arg" not in text

        config = tomllib.loads(text)
        assert config["model"] == "gpt-5"
        assert config["mcp_servers"]["longhouse"]["command"] == "longhouse"
        assert "other" in config["mcp_servers"]

    def test_idempotent(self, tmp_path: Path) -> None:
        """Running twice produces same result."""
        install_codex_mcp_server(codex_dir=str(tmp_path))
        install_codex_mcp_server(codex_dir=str(tmp_path))

        config_path = tmp_path / "config.toml"
        config = tomllib.loads(config_path.read_text())
        assert config["mcp_servers"]["longhouse"]["command"] == "longhouse"

    def test_raises_on_corrupt_toml(self, tmp_path: Path) -> None:
        """Raises RuntimeError on corrupt TOML."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[broken\nnot valid toml")

        with pytest.raises(RuntimeError, match="Failed to parse"):
            install_codex_mcp_server(codex_dir=str(tmp_path))

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        """Creates parent directory if it doesn't exist."""
        codex_dir = tmp_path / "nonexistent" / "codex"
        install_codex_mcp_server(codex_dir=str(codex_dir))

        config_path = codex_dir / "config.toml"
        assert config_path.exists()
        config = tomllib.loads(config_path.read_text())
        assert config["mcp_servers"]["longhouse"]["command"] == "longhouse"

    def test_empty_file_handled(self, tmp_path: Path) -> None:
        """Empty config.toml is handled gracefully."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("")

        install_codex_mcp_server(codex_dir=str(tmp_path))

        config = tomllib.loads(config_path.read_text())
        assert config["mcp_servers"]["longhouse"]["command"] == "longhouse"
