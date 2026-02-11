"""Tests for AGENTS.md, MCP settings, and hooks injection in commis workspaces."""

import json
from pathlib import Path

import pytest
import tomllib

from zerg.services.workspace_manager import inject_agents_md
from zerg.services.workspace_manager import inject_codex_mcp_settings
from zerg.services.workspace_manager import inject_commis_hooks
from zerg.services.workspace_manager import inject_mcp_settings


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def test_inject_creates_claude_dir_and_file(workspace: Path) -> None:
    """Injection creates .claude/CLAUDE.md even with no repo AGENTS.md."""
    result = inject_agents_md(workspace)
    assert result is not None
    assert result.exists()
    assert result.parent.name == ".claude"

    content = result.read_text(encoding="utf-8")
    assert "# Longhouse Context" in content
    assert str(workspace) in content


def test_inject_includes_repo_agents_md(workspace: Path) -> None:
    """Repo-level AGENTS.md is loaded into the instruction chain."""
    agents_md = workspace / "AGENTS.md"
    agents_md.write_text("# My Project\n\nDo things the right way.\n", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "# Repository Instructions" in content
    assert "Do things the right way." in content
    assert "_Source: AGENTS.md_" in content
    assert "# Longhouse Context" in content


def test_inject_prefers_agents_md_over_claude_md(workspace: Path) -> None:
    """AGENTS.md takes priority over CLAUDE.md when both exist."""
    (workspace / "AGENTS.md").write_text("agents content", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("claude content", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "agents content" in content
    assert "claude content" not in content


def test_inject_falls_back_to_claude_md(workspace: Path) -> None:
    """Falls back to CLAUDE.md if AGENTS.md doesn't exist."""
    (workspace / "CLAUDE.md").write_text("claude content", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "claude content" in content
    assert "_Source: CLAUDE.md_" in content


def test_inject_with_project_name(workspace: Path) -> None:
    """Project name is included in Longhouse context."""
    result = inject_agents_md(workspace, project_name="my-cool-project")
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "Project: my-cool-project" in content


def test_inject_appends_to_existing_claude_md(workspace: Path) -> None:
    """Does not overwrite existing .claude/CLAUDE.md — appends Longhouse context."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir()
    existing_claude = claude_dir / "CLAUDE.md"
    existing_claude.write_text("# Existing Instructions\n\nDo not touch.\n", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "# Existing Instructions" in content
    assert "Do not touch." in content
    assert "# Longhouse Context" in content


def test_inject_does_not_duplicate_longhouse_context(workspace: Path) -> None:
    """If Longhouse context already present, skip appending."""
    claude_dir = workspace / ".claude"
    claude_dir.mkdir()
    existing_claude = claude_dir / "CLAUDE.md"
    existing_claude.write_text("# Longhouse Context\n\nAlready here.\n", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    # Should still contain the original, not duplicated
    assert content.count("# Longhouse Context") == 1


def test_inject_loads_global_instructions(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Global instructions from ~/.longhouse/agents.md are loaded."""
    fake_home = tmp_path / "fakehome"
    longhouse_dir = fake_home / ".longhouse"
    longhouse_dir.mkdir(parents=True)
    global_md = longhouse_dir / "agents.md"
    global_md.write_text("# Global\n\nAlways be kind to tests.\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "# Global Instructions" in content
    assert "Always be kind to tests." in content


def test_inject_section_ordering(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sections appear in order: Global, Repository, Longhouse Context."""
    # Set up global instructions
    fake_home = tmp_path / "fakehome"
    longhouse_dir = fake_home / ".longhouse"
    longhouse_dir.mkdir(parents=True)
    (longhouse_dir / "agents.md").write_text("global stuff", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Set up repo instructions
    (workspace / "AGENTS.md").write_text("repo stuff", encoding="utf-8")

    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    global_pos = content.index("# Global Instructions")
    repo_pos = content.index("# Repository Instructions")
    longhouse_pos = content.index("# Longhouse Context")

    assert global_pos < repo_pos < longhouse_pos


def test_inject_mentions_mcp_auto_configured(workspace: Path) -> None:
    """Longhouse context mentions MCP tools are auto-configured."""
    result = inject_agents_md(workspace)
    assert result is not None

    content = result.read_text(encoding="utf-8")
    assert "MCP tools" in content
    assert ".claude/settings.json" in content


# --- inject_mcp_settings tests ---


class TestInjectMcpSettings:
    """Tests for MCP server config injection into .claude/settings.json."""

    def test_creates_file(self, workspace: Path) -> None:
        """Creates .claude/settings.json with MCP config from scratch."""
        result = inject_mcp_settings(workspace)
        assert result is not None
        assert result.exists()
        assert result.name == "settings.json"
        assert result.parent.name == ".claude"

        settings = json.loads(result.read_text(encoding="utf-8"))
        assert "mcpServers" in settings
        assert "longhouse" in settings["mcpServers"]

        lh = settings["mcpServers"]["longhouse"]
        assert lh["type"] == "stdio"
        assert lh["command"] == "longhouse"
        assert lh["args"] == ["mcp-server"]

    def test_preserves_existing_settings(self, workspace: Path) -> None:
        """Existing settings.json content is preserved when injecting MCP config."""
        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {
            "permissions": {"allow": ["Read", "Write"]},
            "mcpServers": {
                "other-server": {"type": "stdio", "command": "other"},
            },
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

        result = inject_mcp_settings(workspace)
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        # Existing keys preserved
        assert settings["permissions"] == {"allow": ["Read", "Write"]}
        assert "other-server" in settings["mcpServers"]
        # Longhouse added
        assert "longhouse" in settings["mcpServers"]

    def test_with_api_url(self, workspace: Path) -> None:
        """--url arg is included when api_url is provided."""
        result = inject_mcp_settings(workspace, api_url="https://david.longhouse.ai")
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        lh = settings["mcpServers"]["longhouse"]
        assert lh["args"] == ["mcp-server", "--url", "https://david.longhouse.ai"]

    def test_overwrites_stale_longhouse_entry(self, workspace: Path) -> None:
        """Existing longhouse MCP entry is replaced with fresh config."""
        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True)
        stale = {
            "mcpServers": {
                "longhouse": {
                    "type": "stdio",
                    "command": "longhouse",
                    "args": ["mcp-server", "--url", "http://old-url:9999"],
                },
            },
        }
        (claude_dir / "settings.json").write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")

        result = inject_mcp_settings(workspace, api_url="http://new-url:8080")
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        lh = settings["mcpServers"]["longhouse"]
        assert lh["args"] == ["mcp-server", "--url", "http://new-url:8080"]
        assert "old-url" not in json.dumps(settings)

    def test_without_api_url(self, workspace: Path) -> None:
        """No --url arg when api_url is None."""
        result = inject_mcp_settings(workspace, api_url=None)
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        lh = settings["mcpServers"]["longhouse"]
        assert lh["args"] == ["mcp-server"]

    def test_handles_corrupt_json(self, workspace: Path) -> None:
        """Corrupt settings.json is overwritten cleanly."""
        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("not valid json {{{", encoding="utf-8")

        result = inject_mcp_settings(workspace)
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        assert "longhouse" in settings["mcpServers"]


# --- inject_codex_mcp_settings tests ---


class TestInjectCodexMcpSettings:
    """Tests for Codex MCP server config injection into .codex/config.toml."""

    def test_creates_file(self, workspace: Path) -> None:
        """Creates .codex/config.toml with MCP config from scratch."""
        result = inject_codex_mcp_settings(workspace)
        assert result is not None
        assert result.exists()
        assert result.name == "config.toml"
        assert result.parent.name == ".codex"

        config = tomllib.loads(result.read_text(encoding="utf-8"))
        assert "mcp_servers" in config
        assert "longhouse" in config["mcp_servers"]

        lh = config["mcp_servers"]["longhouse"]
        assert lh["command"] == "longhouse"
        assert lh["args"] == ["mcp-server"]

    def test_preserves_existing_config(self, workspace: Path) -> None:
        """Existing config.toml content is preserved when injecting MCP config."""
        codex_dir = workspace / ".codex"
        codex_dir.mkdir(parents=True)
        existing = (
            'model = "gpt-5.2-codex"\n'
            "\n"
            "[mcp_servers.other-server]\n"
            'command = "other"\n'
            'args = ["serve"]\n'
        )
        (codex_dir / "config.toml").write_text(existing, encoding="utf-8")

        result = inject_codex_mcp_settings(workspace)
        assert result is not None

        config = tomllib.loads(result.read_text(encoding="utf-8"))
        # Existing keys preserved
        assert config["model"] == "gpt-5.2-codex"
        assert "other-server" in config["mcp_servers"]
        # Longhouse added
        assert "longhouse" in config["mcp_servers"]
        assert config["mcp_servers"]["longhouse"]["command"] == "longhouse"

    def test_with_api_url(self, workspace: Path) -> None:
        """api_url is included in args when provided."""
        result = inject_codex_mcp_settings(workspace, api_url="https://david.longhouse.ai")
        assert result is not None

        config = tomllib.loads(result.read_text(encoding="utf-8"))
        lh = config["mcp_servers"]["longhouse"]
        assert lh["args"] == ["mcp-server", "--url", "https://david.longhouse.ai"]

    def test_overwrites_stale_longhouse_entry(self, workspace: Path) -> None:
        """Existing longhouse MCP entry is replaced with fresh config."""
        codex_dir = workspace / ".codex"
        codex_dir.mkdir(parents=True)
        stale = (
            "[mcp_servers.longhouse]\n"
            'command = "old-longhouse"\n'
            'args = ["mcp-server", "--url", "http://old-url:9999"]\n'
        )
        (codex_dir / "config.toml").write_text(stale, encoding="utf-8")

        result = inject_codex_mcp_settings(workspace)
        assert result is not None

        config = tomllib.loads(result.read_text(encoding="utf-8"))
        lh = config["mcp_servers"]["longhouse"]
        assert lh["command"] == "longhouse"
        assert lh["args"] == ["mcp-server"]
        text = result.read_text(encoding="utf-8")
        assert "old-longhouse" not in text
        assert "old-url" not in text

    def test_without_api_url(self, workspace: Path) -> None:
        """No --url arg when api_url is None."""
        result = inject_codex_mcp_settings(workspace, api_url=None)
        assert result is not None

        config = tomllib.loads(result.read_text(encoding="utf-8"))
        lh = config["mcp_servers"]["longhouse"]
        assert lh["args"] == ["mcp-server"]

    def test_handles_corrupt_toml(self, workspace: Path) -> None:
        """Corrupt config.toml is overwritten cleanly."""
        codex_dir = workspace / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text("[broken\nnot valid toml", encoding="utf-8")

        result = inject_codex_mcp_settings(workspace)
        assert result is not None

        config = tomllib.loads(result.read_text(encoding="utf-8"))
        assert "longhouse" in config["mcp_servers"]


# --- inject_commis_hooks tests ---


class TestInjectCommisHooks:
    """Tests for quality-gate hook injection into .claude/settings.json."""

    def test_creates_hooks_with_makefile(self, workspace: Path) -> None:
        """When Makefile exists, Stop hook runs 'make test'."""
        (workspace / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")

        result = inject_commis_hooks(workspace)
        assert result is not None
        assert result.exists()

        settings = json.loads(result.read_text(encoding="utf-8"))
        assert "hooks" in settings
        assert "Stop" in settings["hooks"]

        stop_hooks = settings["hooks"]["Stop"]
        # Should have verify hook + notification hook
        assert len(stop_hooks) == 2

        # First hook: verify command
        verify_entry = stop_hooks[0]
        assert verify_entry["matcher"] == ""
        assert len(verify_entry["hooks"]) == 1
        assert verify_entry["hooks"][0]["type"] == "command"
        assert "make test" in verify_entry["hooks"][0]["command"]
        assert verify_entry["hooks"][0]["timeout"] == 300

    def test_skips_verify_without_makefile(self, workspace: Path) -> None:
        """When no Makefile exists, only the notification hook is injected."""
        result = inject_commis_hooks(workspace)
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        stop_hooks = settings["hooks"]["Stop"]
        # Only notification hook, no verify
        assert len(stop_hooks) == 1
        assert "notify" in stop_hooks[0]["hooks"][0]["command"].lower() or "Oikos" in stop_hooks[0]["hooks"][0]["command"]

    def test_custom_verify_command(self, workspace: Path) -> None:
        """Custom verify_command overrides Makefile heuristic."""
        result = inject_commis_hooks(workspace, verify_command="npm test")
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        stop_hooks = settings["hooks"]["Stop"]
        assert len(stop_hooks) == 2
        assert stop_hooks[0]["hooks"][0]["command"] == "npm test"

    def test_preserves_existing_mcp_settings(self, workspace: Path) -> None:
        """Existing MCP config in settings.json is preserved when adding hooks."""
        # First inject MCP settings
        inject_mcp_settings(workspace, api_url="http://localhost:8080")

        # Then inject hooks
        (workspace / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        result = inject_commis_hooks(workspace)
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        # MCP config still present
        assert "mcpServers" in settings
        assert "longhouse" in settings["mcpServers"]
        # Hooks also present
        assert "hooks" in settings
        assert "Stop" in settings["hooks"]

    def test_preserves_other_hook_events(self, workspace: Path) -> None:
        """Existing hooks for other events (PreToolUse, etc.) are preserved."""
        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo pre-check"}],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

        result = inject_commis_hooks(workspace)
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        # PreToolUse preserved
        assert "PreToolUse" in settings["hooks"]
        assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
        # Stop hooks added
        assert "Stop" in settings["hooks"]

    def test_notification_hook_is_async(self, workspace: Path) -> None:
        """The notification hook runs async (non-blocking)."""
        result = inject_commis_hooks(workspace)
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        # Last Stop hook is the notification hook
        notify_entry = settings["hooks"]["Stop"][-1]
        assert notify_entry["hooks"][0].get("async") is True

    def test_handles_corrupt_json(self, workspace: Path) -> None:
        """Corrupt settings.json is overwritten cleanly with hooks."""
        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("not valid json {{{", encoding="utf-8")

        result = inject_commis_hooks(workspace, verify_command="pytest")
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        assert "hooks" in settings
        assert len(settings["hooks"]["Stop"]) == 2

    def test_overwrites_existing_stop_hooks(self, workspace: Path) -> None:
        """Existing Stop hooks are replaced with fresh config."""
        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True)
        stale = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "old-verify"}],
                    }
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")

        result = inject_commis_hooks(workspace, verify_command="new-verify")
        assert result is not None

        settings = json.loads(result.read_text(encoding="utf-8"))
        stop_hooks = settings["hooks"]["Stop"]
        assert stop_hooks[0]["hooks"][0]["command"] == "new-verify"
        assert "old-verify" not in json.dumps(settings)
