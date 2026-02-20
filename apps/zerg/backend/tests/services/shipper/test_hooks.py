"""Tests for hook installation and MCP server registration (hooks.py)."""

import json
from pathlib import Path

import pytest
import tomllib

from zerg.services.shipper.hooks import install_codex_mcp_server
from zerg.services.shipper.hooks import install_hooks
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


class TestInstallHooks:
    """Tests for install_hooks() — unified hook script + settings.json registration."""

    def test_writes_hook_scripts(self, tmp_path: Path) -> None:
        """longhouse-hook.sh and longhouse-session-start.sh are created."""
        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        hooks_dir = tmp_path / "hooks"
        assert (hooks_dir / "longhouse-hook.sh").exists()
        assert (hooks_dir / "longhouse-session-start.sh").exists()

    def test_does_not_write_old_scripts(self, tmp_path: Path) -> None:
        """Deprecated longhouse-ship.sh and longhouse-presence.sh are NOT written."""
        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        hooks_dir = tmp_path / "hooks"
        assert not (hooks_dir / "longhouse-ship.sh").exists()
        assert not (hooks_dir / "longhouse-presence.sh").exists()

    def test_removes_deprecated_scripts(self, tmp_path: Path) -> None:
        """Pre-existing deprecated scripts are removed during install."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "longhouse-ship.sh").write_text("#!/bin/bash\necho old\n")
        (hooks_dir / "longhouse-presence.sh").write_text("#!/bin/bash\necho old\n")

        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        assert not (hooks_dir / "longhouse-ship.sh").exists(), "deprecated ship.sh must be removed"
        assert not (hooks_dir / "longhouse-presence.sh").exists(), "deprecated presence.sh must be removed"

    def test_registers_all_events(self, tmp_path: Path) -> None:
        """Stop, UserPromptSubmit, PreToolUse, PostToolUse, and SessionStart are all registered."""
        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        settings = json.loads((tmp_path / "settings.json").read_text())
        hooks = settings.get("hooks", {})
        for event in ("Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionStart"):
            assert event in hooks, f"{event} must be registered"
            assert len(hooks[event]) > 0, f"{event} must have at least one entry"

    def test_no_async_true(self, tmp_path: Path) -> None:
        """No Longhouse hook registration has async: true."""
        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        settings = json.loads((tmp_path / "settings.json").read_text())
        hooks = settings.get("hooks", {})
        for event, entries in hooks.items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    cmd = hook.get("command", "")
                    if "longhouse" in cmd:
                        assert not hook.get("async", False), (
                            f"Longhouse hook on {event} must not have async: true"
                        )

    def test_hook_script_uses_outbox(self, tmp_path: Path) -> None:
        """The unified hook script writes to outbox, not curl."""
        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        hook_content = (tmp_path / "hooks" / "longhouse-hook.sh").read_text()
        assert "outbox" in hook_content, "hook must write to outbox directory"
        assert "curl" not in hook_content, "hook must not make direct HTTP calls"

    def test_engine_path_baked_in(self, tmp_path: Path) -> None:
        """Custom engine path is baked into the hook script."""
        install_hooks(
            url="https://example.longhouse.ai",
            claude_dir=str(tmp_path),
            engine_path="/custom/path/longhouse-engine",
        )

        hook_content = (tmp_path / "hooks" / "longhouse-hook.sh").read_text()
        assert "/custom/path/longhouse-engine" in hook_content

    def test_idempotent(self, tmp_path: Path) -> None:
        """Running twice produces same settings.json (no duplicates)."""
        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))
        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        settings = json.loads((tmp_path / "settings.json").read_text())
        hooks = settings.get("hooks", {})
        # Each event should have exactly one Longhouse entry
        for event in ("Stop", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
            longhouse_entries = [
                e for e in hooks.get(event, [])
                if any("longhouse" in h.get("command", "") for h in e.get("hooks", []))
            ]
            assert len(longhouse_entries) == 1, (
                f"{event} should have exactly 1 Longhouse entry, got {len(longhouse_entries)}"
            )

    def test_preserves_other_hooks(self, tmp_path: Path) -> None:
        """Non-Longhouse hooks in settings.json are preserved."""
        settings_path = tmp_path / "settings.json"
        existing = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "/usr/local/bin/my-hook.sh"}]}
                ]
            }
        }
        settings_path.write_text(json.dumps(existing))

        install_hooks(url="https://example.longhouse.ai", claude_dir=str(tmp_path))

        settings = json.loads(settings_path.read_text())
        stop_entries = settings["hooks"]["Stop"]
        # Should have both the user's hook and the Longhouse hook
        commands = [
            h.get("command", "")
            for entry in stop_entries
            for h in entry.get("hooks", [])
        ]
        assert any("my-hook.sh" in c for c in commands), "user hook must be preserved"
        assert any("longhouse" in c for c in commands), "Longhouse hook must be present"
